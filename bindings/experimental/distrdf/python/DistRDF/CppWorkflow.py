# @author Enric Tejedor
# @date 2021-07

################################################################################
# Copyright (C) 1995-2021, Rene Brun and Fons Rademakers.                      #
# All rights reserved.                                                         #
#                                                                              #
# For the licensing terms see $ROOTSYS/LICENSE.                                #
# For the list of contributors see $ROOTSYS/README/CREDITS.                    #
################################################################################

import os
from copy import deepcopy
from functools import singledispatch
from typing import Dict, List, NamedTuple, Tuple
from textwrap import dedent

from DistRDF.Node import Node
from DistRDF.Operation import Action, AsNumpy, Operation, Snapshot
from DistRDF.PythonMergeables import SnapshotResult

import ROOT


class _SnapshotData(NamedTuple):
    res_id: int
    treename: str
    filename: str


class _PyActionData(NamedTuple):
    res_id: int
    operation: Operation


@singledispatch
def _create_lazy_op_if_needed(operation: Operation, _: int) -> Operation:
    return operation


@_create_lazy_op_if_needed.register
def _(operation: AsNumpy, _: int) -> AsNumpy:
    operation.kwargs["lazy"] = True
    return operation


@_create_lazy_op_if_needed.register
def _(operation: Snapshot, range_id: int) -> Snapshot:

    op_modified = deepcopy(operation)

    # Retrieve filename and append range boundaries
    filename = op_modified.args[1].partition(".root")[0]
    path_with_range = "{}_{}.root".format(filename, range_id)
    # Create a partial snapshot on the current range
    op_modified.args[1] = path_with_range

    if len(op_modified.args) == 2:
        # Only the first two mandatory arguments were passed
        # Only the following overload is possible
        # Snapshot(std::string_view treename, std::string_view filename, std::string_view columnNameRegexp = "")
        op_modified.args.append("")  # Append empty regex

    # Processing of RSnapshotOptions is very limited at the moment.
    # An instance is created in the generated C++ code, with lazy option enabled
    # by default. Here we just write a string in the position of the
    # RSnapshotOptions argument, which will be detected when the code for this
    # operation is generated down the line
    if len(op_modified.args) == 4:
        op_modified.args[3] = "lazy_options"
    else:
        op_modified.args.append("lazy_options")  # Append RSnapshotOptions

    return op_modified


class CppWorkflow(object):
    '''
    Class that encapsulates the generation of the code of an RDataFrame workflow
    in C++, together with its compilation into a shared library and execution.

    This class is used by worker processes to execute in C++ the RDataFrame
    graph they receive. This is done for the sake of performance, since running
    the graph from Python necessarily relies on jitted code, which is less
    optimized and thus slower than a shared library compiled with ACLiC.

    Attributes:
        _CACHED_WFS (dict): uses as key the code of workflow functions that have
            been already compiled and loaded by the current process, while the
            value is the id of a given workflow function. Used to prevent
            recompilation of already executed workflow functions.

        _FUNCTION_NAME (string): name of the function that encapsulates the
            RDataFrame graph creation

        _FUNCTION_NAMESPACE (string): namespace of the function that
            encapsulates the RDataFrame graph creation

        graph_nodes (Dict[int, Node]): The nodes of the computation graph.

        range_id (int): The id of the current range being processed.

        starting_node (ROOT.RDF.RNode): A reference to the C++ headnode of the
            computation graph.

        _nodes_code (str): The part of the generated C++ code that includes the
            representation of the nodes of the computation graph.

        _application_code (str): The final generated C++ application.

        _includes (string): include statements needed by the workflow.

        _lambdas (string): lambda functions used by the workflow.

        _lambda_id (int): counter used to generate ids for each defined lambda
            function.

        _node_id (int): counter used to generate ids for each graph node.

        _py_actions (list): list that contains _PyActionData objects.

        _res_ptr_id (int): counter used to generate ids for each result
            generated by graph actions.

        _snapshots (list): list that contains _SnapshotData objects

        _WF_ID_COUNTER (int): used to assign new ids to workflow functions
    '''

    _FUNCTION_NAME = '__RDF_WORKFLOW_FUNCTION__'
    _FUNCTION_NAMESPACE = 'DistRDF_Internal'

    _CACHED_WFS: Dict[str, int] = {}
    _WF_ID_COUNTER: int = 0

    def __init__(self, graph_nodes: Dict[int, "Node"], starting_node: ROOT.RDF.RNode, range_id: int):
        '''
        Generates the C++ code of an RDF workflow that corresponds to the
        received graph and data range.

        Args:
            head_node (Node): head node of a graph that represents an RDF
                workflow.
            range_id (int): id of the data range to be processed by this
                workflow. Needed to assign a name to a partial Snapshot output
                file.
        '''

        self._includes = dedent('''
            #include <tuple>
            #include <utility>
            #include <vector>
            #include "ROOT/RDataFrame.hxx"
            #include "ROOT/RDFHelpers.hxx"
            #include "ROOT/RResultHandle.hxx"
            ''')
        self._lambdas = ''
        self._lambda_id = 0
        self._snapshots = []
        self._py_actions = []

        # Generated C++ code with only the nodes of the computation graph
        self._nodes_code: str = ""
        # Counter to keep track of how many results the workflow is
        # creating. Needed for the AsNumpy operation
        self._res_ptr_id: int = 0
        # Full generated C++ application
        self._application_code: str = None

        self.graph_nodes = graph_nodes
        self.starting_node = ROOT.RDF.AsRNode(starting_node)
        self.range_id = range_id

        self._handle_op = singledispatch(self._handle_op)
        self._handle_op.register(Action, self._handle_action)
        self._handle_op.register(Snapshot, self._handle_snapshot)
        self._handle_op.register(AsNumpy, self._handle_asnumpy)

        # Generate the C++ workflow.
        self._generate_computation_graph()

    @property
    def application_code(self) -> str:
        """Gather the full C++ application code in a string."""

        if self._application_code is None:
            # Gather the code for this instance only once
            self._application_code = self._get_code()
        return self._application_code

    def __repr__(self) -> str:
        '''
        Generates a string representation for this C++ workflow.
        '''

        return self.application_code

    def _add_node(self, operation: Operation, node_id: int, parent_id: int):
        """
        Generates the C++ code for a single node of the graph and adds it to the
        internal string representation. Operations are first made lazy and the
        Snapshot operation has special treatment to change the output filename.
        """

        operation = _create_lazy_op_if_needed(operation, self.range_id)
        self._handle_op(operation, node_id, parent_id)

    def _compile(self) -> int:
        '''
        Generates the workflow code C++ file and compiles it with ACLiC
        into a shared library. The library is also loaded as part of the
        `TSystem::CompileMacro` call.

        The name of the generated C++ file contains both a hash of its
        code and the ID of the process that created it. This is done to
        prevent clashes between multiple (non-sandboxed) worker processes
        that try to write to the same file concurrently.

        A class-level cache keeps track of the workflows that have been already
        compiled to prevent unncessary recompilation (e.g. when a worker
        process runs multiple times the same workflow).

        Returns:
            int: the id of the workflow function to be executed. Such id is
                appended to CppWorkflow._FUNCTION_NAME to prevent name clashes
                (a worker process might compile and load multiple workflow
                functions).
        '''

        # TODO: Make this function thread-safe? To support Dask threaded
        # workers

        code = self.application_code
        this_wf_id = CppWorkflow._CACHED_WFS.get(code)
        if this_wf_id is not None:
            # We already compiled and loaded a workflow function with this
            # code. Return the id of that function
            return this_wf_id

        # We are trying to run this workflow for the first time in this
        # process. First dump the code in a file with the right function name
        this_wf_id = CppWorkflow._WF_ID_COUNTER
        cpp_file_name = 'rdfworkflow_{wf_id}_{pid}.cxx' \
                        .format(wf_id=this_wf_id, pid=os.getpid())
        final_code = code.replace(CppWorkflow._FUNCTION_NAME, CppWorkflow._FUNCTION_NAME + str(this_wf_id), 1)

        with open(cpp_file_name, 'w') as f:
            f.write(final_code)

        # Now compile and load the code
        if not ROOT.gSystem.CompileMacro(cpp_file_name, 'O'):
            raise RuntimeError(f"Error compiling the RDataFrame workflow file: {cpp_file_name}")

        # Let the cache know there is a new workflow
        CppWorkflow._CACHED_WFS[code] = this_wf_id
        CppWorkflow._WF_ID_COUNTER += 1

        return this_wf_id

    def _generate_computation_graph(self):
        """
        Generates the RDataFrame computation graph from the nodes stored in the
        input graph.
        """

        # Iterate over the other nodes stored in the dictionary, skipping the head
        # node. We can iterate over the values knowing that the dictionary preserves
        # the order in which it was created. Thus, we traverse the graph from top
        # to bottom, in order to create the RDF nodes in the right order.
        nodes = iter(self.graph_nodes.items())
        _ = next(nodes)

        for node_id, node in nodes:
            self._add_node(node.operation, node_id, node.parent_id)

    def _get_args_call(self, operation: Operation) -> str:
        '''
        Gets the arguments with which to generate the call to a given operation.

        Args:
            operation (Operation): object representing the operation whose
                call arguments need to be returned.

        Returns:
            string: call arguments for this operation.
        '''

        # TODO
        # - Do a more thorough type conversion
        # - Use RDF helper functions to convert jitted strings to lambdas

        args = ""

        # Argument type conversion
        for narg, arg in enumerate(operation.args):
            if (narg > 0):
                args += ', '

            if arg == "lazy_options":
                args += "lazy_options"
            elif isinstance(arg, str):
                args += '"{}"'.format(arg)
            elif isinstance(arg, tuple):
                args += '{'
                for nelem, elem in enumerate(arg):
                    if nelem > 0:
                        args += ','
                    if isinstance(elem, str):
                        args += '"{}"'.format(elem)
                    else:
                        args += '{}'.format(elem)
                args += '}'

        return args

    def _get_args_template(self, operation: Operation) -> str:
        '''
        Gets the template arguments with which to generate the call to a given
        operation.

        Args:
            operation (Operation): object representing the operation whose
                template arguments need to be returned.

        Returns:
            string: template arguments for this operation.
        '''

        # TODO: generate templated operations when possible, e.g. Max<double>

        return ''

    def _get_code(self) -> str:
        '''
        Composes the workflow generation code from the different attributes
        of this class. The resulting code contains a function that will be
        called to generate the RDataFrame graph. Such function returns a tuple
        of three elements:
        1. A vector of results of the graph actions.
        2. A vector with the result types of those actions.
        3. A vector of RDF nodes that will be used in Python to invoke
        Python-only actions on them (e.g. `AsNumpy`).
        '''

        code = '''
{includes}

namespace {namespace} {{

using CppWorkflowResult = std::tuple<std::vector<ROOT::RDF::RResultHandle>,
                          std::vector<std::string>,
                          std::vector<ROOT::RDF::RNode>>;

CppWorkflowResult {func_name}(ROOT::RDF::RNode &node0)
{{
  std::vector<ROOT::RDF::RResultHandle> result_handles;
  std::vector<std::string> result_types;
  std::vector<ROOT::RDF::RNode> output_nodes;

  // To make Snapshots lazy
  ROOT::RDF::RSnapshotOptions lazy_options;
  lazy_options.fLazy = true;

{lambdas}

{nodes}

  return {{ std::move(result_handles), std::move(result_types), std::move(output_nodes) }};
}}

}} // namespace {namespace}
'''.format(func_name=CppWorkflow._FUNCTION_NAME,
           namespace=CppWorkflow._FUNCTION_NAMESPACE,
           includes=self._includes,
           lambdas=self._lambdas,
           nodes=self._nodes_code)

        return code

    def _handle_action(self, operation: Action, node_id: int, parent_id: int):
        """
        Generates the code for an Action operation. This needs the definition of
        the node running the operation as in the generic case, plus storing the
        RResultPtr into the vector of RResultHandles.
        """

        self._handle_op.dispatch(Operation)(operation, node_id, parent_id)

        # The result is stored in the vector of results to be returned
        self._nodes_code += f"\n  result_handles.emplace_back(node{node_id});"

        # The result type is stored in the vector of result types to be
        # returned
        err = f"Cannot get type of result {node_id} of action {operation.name} during generation of RDF C++ workflow"
        self._nodes_code += (
            f'\n  auto c{node_id} = TClass::GetClass(typeid(node{node_id}));'
            f'\n  if (c{node_id} == nullptr)'
            f'\n    throw std::runtime_error("{err}");'
            f'\n  result_types.emplace_back(c{node_id}->GetName());'
        )

        self._res_ptr_id += 1

    def _handle_asnumpy(self, operation: AsNumpy, node_id: int, parent_id: int):
        '''
        Since AsNumpy is a Python-only action, it can't be included in the
        C++ workflow built by this class. Therefore, this function takes care
        of saving the RDF node, generated in C++, on which an AsNumpy action
        should be applied from Python.
        '''

        # Store DFS-order index of the AsNumpy operation, together with the
        # operation information, for later invocation from Python
        self._py_actions.append(_PyActionData(self._res_ptr_id, operation))

        # Save parent RDF node to run AsNumpy on it later from Python
        self._nodes_code += f"\n  output_nodes.push_back(ROOT::RDF::AsRNode(node{parent_id}));"

        # Add placeholders to the result lists
        self._nodes_code += (
            "\n  result_handles.emplace_back();"
            "\n  result_types.emplace_back();"
        )

        self._res_ptr_id += 1

    def _handle_op(self, operation: Operation, node_id: int, parent_id: int):
        """
        Generates the code for a generic operation.
        """

        op_call = (
            f"node{parent_id}.{operation.name}{self._get_args_template(operation)}"
            f"({self._get_args_call(operation)})"
        )

        self._nodes_code += f"\n  auto node{node_id} = {op_call};"

    def _handle_snapshot(self, operation: Snapshot, node_id: int, parent_id: int):
        '''
        Generates the code for the Snapshot operation. Stores the index of the
        returned vector<RResultHandle> in which the result of this Snapshot is
        stored, together with the modified file path.
        '''

        self._snapshots.append(_SnapshotData(self._res_ptr_id, operation.args[0], operation.args[1]))
        self._handle_action(operation, node_id, parent_id)

    def _run_function(self, wf_id: int) -> Tuple[List, List[str]]:
        '''
        Runs the workflow generation function.

        Args:
            wf_id (int): identifier of the workflow function to be executed.

        Returns:
            tuple: the first element is the list of results of the actions in
                the C++ workflow, the second element is the list of result types
                corresponding to those actions.
        '''

        ns = getattr(ROOT, CppWorkflow._FUNCTION_NAMESPACE)
        func = getattr(ns, CppWorkflow._FUNCTION_NAME + str(wf_id))

        # Run the workflow generator function
        vectors = func(self.starting_node)  # need to keep the tuple alive
        v_results, v_res_types, v_nodes = vectors

        # Convert the vector of results into a list so that we can mix
        # different types in it.
        # We copy the results since the life of the original ones is tied to
        # that of the vector
        results = [ROOT.RDF.RResultHandle(res) for res in v_results]

        # Strip out the ROOT::RDF::RResultPtr<> part of the type
        def get_result_type(s):
            if s.empty():
                # Python-only actions have an empty return type in C++
                return ''

            s = str(s)
            pos = s.find('<')
            if pos == -1:
                raise RuntimeError(
                    'Error parsing the result types of RDataFrame workflow')
            return s[pos+1:-1].strip()

        res_types = [get_result_type(elem) for elem in v_res_types]

        # Add Python-only actions on their corresponding nodes
        for (res_ptr_id, operation), n in zip(self._py_actions, v_nodes):
            operation.kwargs['lazy'] = True  # make it lazy
            results[res_ptr_id] = getattr(n, operation.name)(*operation.args, **operation.kwargs)

        if v_results:
            # We trigger the event loop here, so make sure we release the GIL
            RunGraphs = ROOT.RDF.RunGraphs
            old_rg = RunGraphs.__release_gil__
            RunGraphs.__release_gil__ = True
            RunGraphs(v_results)
            RunGraphs.__release_gil__ = old_rg

        # Replace the RResultHandle of each Snapshot by its modified output
        # path, since the latter is what we actually need in the reducer
        for res_ptr_id, treename, path in self._snapshots:
            results[res_ptr_id] = SnapshotResult(treename, [path])
            res_types[res_ptr_id] = None  # placeholder

        # AsNumpyResult needs to be triggered before being merged
        for i, operation in self._py_actions:
            results[i].GetValue()

        return results, res_types

    def execute(self) -> Tuple[List, List[str]]:
        '''
        Compiles the workflow generation code and executes it.

        Returns:
            tuple: the first element is the list of results of the actions in
                the C++ workflow, the second element is the list of result types
                corresponding to those actions.
        '''

        wf_id = self._compile()
        res = self._run_function(wf_id)
        # TODO: it would be nice to remove all created artifacts
        # after creation of the shared library. This is blocked by #10640
        return res
