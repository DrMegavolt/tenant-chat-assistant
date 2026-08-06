"""The dispatcher graph: nodes, edges, and the version they add up to.

The shape is small on purpose. Every node is a checkpoint write, and `ADR-0001`
accepted that single-turn chat pays graph overhead it does not need — so the
budget is spent where resumability is actually worth something, which is the
booking confirmation, and nowhere else. A question that calls no tools crosses
three nodes: route, model, finalize.

``GRAPH_VERSION`` changes whenever the node set, the edges, or a node's
observable behavior changes. `OBS-004` records it against every turn, which is
what makes "answers got worse last Tuesday" a question with an answer.
"""

from __future__ import annotations

from typing import Final

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer

from tenantchat.orchestration.dependencies import DispatchDependencies
from tenantchat.orchestration.nodes import (
    DispatchNode,
    DispatchNodes,
    route_after_confirmation,
    route_after_model,
    route_after_routing,
    route_after_tools,
)
from tenantchat.orchestration.state import DispatchState

GRAPH_VERSION: Final = "dispatch@2"


DispatchGraph = StateGraph[DispatchState, None, DispatchState, DispatchState]
CompiledDispatchGraph = CompiledStateGraph[DispatchState, None, DispatchState, DispatchState]


def build_dispatch_graph(dependencies: DispatchDependencies) -> DispatchGraph:
    """Assemble the graph without compiling it.

    Returned uncompiled so a caller chooses the checkpointer — the same graph
    definition runs against PostgreSQL in a deployment and against an in-memory
    saver in a test, and neither is the graph's business.
    """
    nodes = DispatchNodes(dependencies)
    builder: DispatchGraph = StateGraph(DispatchState)

    builder.add_node(DispatchNode.ROUTE.value, nodes.route)
    builder.add_node(DispatchNode.MODEL.value, nodes.call_model)
    builder.add_node(DispatchNode.TOOLS.value, nodes.run_tools)
    builder.add_node(DispatchNode.CONFIRM_BOOKING.value, nodes.confirm_booking)
    builder.add_node(DispatchNode.COMMIT_BOOKING.value, nodes.commit_booking)
    builder.add_node(DispatchNode.ESCALATE.value, nodes.escalate)
    builder.add_node(DispatchNode.FINALIZE.value, nodes.finalize)

    builder.add_edge(START, DispatchNode.ROUTE.value)
    builder.add_conditional_edges(
        DispatchNode.ROUTE.value,
        route_after_routing,
        [DispatchNode.MODEL.value, DispatchNode.ESCALATE.value, DispatchNode.FINALIZE.value],
    )
    builder.add_conditional_edges(
        DispatchNode.MODEL.value,
        route_after_model,
        [
            DispatchNode.TOOLS.value,
            DispatchNode.CONFIRM_BOOKING.value,
            DispatchNode.ESCALATE.value,
            DispatchNode.FINALIZE.value,
        ],
    )
    builder.add_conditional_edges(
        DispatchNode.TOOLS.value,
        route_after_tools,
        [DispatchNode.MODEL.value, DispatchNode.CONFIRM_BOOKING.value],
    )
    builder.add_conditional_edges(
        DispatchNode.CONFIRM_BOOKING.value,
        route_after_confirmation,
        [DispatchNode.MODEL.value, DispatchNode.COMMIT_BOOKING.value],
    )
    builder.add_edge(DispatchNode.COMMIT_BOOKING.value, DispatchNode.MODEL.value)
    builder.add_edge(DispatchNode.ESCALATE.value, END)
    builder.add_edge(DispatchNode.FINALIZE.value, END)

    return builder


def compile_dispatch_graph(
    dependencies: DispatchDependencies,
    checkpointer: Checkpointer,
) -> CompiledDispatchGraph:
    """Compile the graph against a checkpointer.

    The checkpointer is required rather than optional. A graph compiled without
    one cannot be interrupted or resumed, which would make the booking
    confirmation silently stop working the moment someone forgot to pass it.
    """
    if checkpointer is None:
        raise ValueError("the dispatcher graph requires a checkpointer to interrupt and resume")
    return build_dispatch_graph(dependencies).compile(checkpointer=checkpointer)
