# coding: utf-8

"""
Exemplary selection methods.
"""

from collections import defaultdict

from columnflow.selection import Selector, SelectionResult, selector
from columnflow.selection.stats import increment_stats
from columnflow.production.processes import process_ids
from columnflow.production.cms.mc_weight import mc_weight
from columnflow.util import maybe_import

from __cf_module_name__.production.example import cutflow_features


np = maybe_import("numpy")
ak = maybe_import("awkward")


#
# selectors used by categories definitions
# (not "exposed" to be used from the command line)
#

@selector(uses={"event"})
def sel_incl(self: Selector, events: ak.Array, **kwargs) -> ak.Array:
    # fully inclusive selection
    return ak.ones_like(events.event) == 1


@selector(uses={"Jet.pt"})
def sel_2j(self: Selector, events: ak.Array, **kwargs) -> ak.Array:
    # two or more jets
    return ak.num(events.Jet.pt, axis=1) >= 2


#
# other unexposed selectors
# (not selectable from the command line but used by other, exposed selectors)
#


@selector(
    uses={"Muon.pt", "Muon.eta"},
)
def muon_selection(
    self: Selector,
    events: ak.Array,
    **kwargs,
) -> tuple[ak.Array, SelectionResult]:
    # example muon selection: exactly one muon
    muon_mask = (events.Muon.pt >= 20.0) & (abs(events.Muon.eta) < 2.1)
    muon_sel = ak.sum(muon_mask, axis=1) == 1

    # build and return selection results
    # "objects" maps source columns to new columns and selections to be applied on the old columns
    # to create them, e.g. {"Muon": {"MySelectedMuon": indices_applied_to_Muon}}
    return events, SelectionResult(
        steps={
            "muon": muon_sel,
        },
        objects={
            "Muon": {
                "Muon": muon_mask,
            },
        },
    )


@selector(
    uses={"Jet.pt", "Jet.eta"},
)
def jet_selection(
    self: Selector,
    events: ak.Array,
    **kwargs,
) -> tuple[ak.Array, SelectionResult]:
    # example jet selection: at least one jet
    jet_mask = (events.Jet.pt >= 25.0) & (abs(events.Jet.eta) < 2.4)
    jet_sel = ak.sum(jet_mask, axis=1) >= 1

    # creat pt sorted jet indices
    jet_indices = ak.argsort(events.Jet.pt, axis=-1, ascending=False)
    jet_indices = jet_indices[jet_mask[jet_indices]]

    # build and return selection results
    # "objects" maps source columns to new columns and selections to be applied on the old columns
    # to create them, e.g. {"Jet": {"MyCustomJetCollection": indices_applied_to_Jet}}
    return events, SelectionResult(
        steps={
            "jet": jet_sel,
        },
        objects={
            "Jet": {
                "Jet": jet_indices,
            },
        },
        aux={
            "n_jets": ak.sum(jet_mask, axis=1),
        },
    )


#
# exposed selectors
# (those that can be invoked from the command line)
#

@selector(
    uses={
        # selectors / producers called within _this_ selector
        mc_weight, cutflow_features, process_ids, muon_selection, jet_selection,
        increment_stats,
    },
    produces={
        # selectors / producers whose newly created columns should be kept
        mc_weight, cutflow_features, process_ids,
    },
    exposed=True,
)
def example(
    self: Selector,
    events: ak.Array,
    stats: defaultdict,
    **kwargs,
) -> tuple[ak.Array, SelectionResult]:
    # prepare the selection results that are updated at every step
    results = SelectionResult()

    # muon selection
    events, muon_results = self[muon_selection](events, **kwargs)
    results += muon_results

    # jet selection
    events, jet_results = self[jet_selection](events, **kwargs)
    results += jet_results

    # combined event selection after all steps
    results.main["event"] = results.steps.muon & results.steps.jet

    # create process ids
    events = self[process_ids](events, **kwargs)

    # add the mc weight
    if self.dataset_inst.is_mc:
        events = self[mc_weight](events, **kwargs)

    # add cutflow features, passing per-object masks
    events = self[cutflow_features](events, results.objects, **kwargs)

    # increment stats
    weight_map = {}
    group_map = {}
    group_combinations = []
    if self.dataset_inst.is_mc:
        # mc weight for all events
        weight_map["mc_weight"] = (events.mc_weight, Ellipsis)
        # mc weight for selected events
        weight_map["mc_weight_selected"] = (events.mc_weight, results.main.event)
        # store all weights per process id
        group_map["process"] = {
            "values": events.process_id,
            "mask_fn": (lambda v: events.process_id == v),
        }
        # store all weights per jet multiplicity
        group_map["njet"] = {
            "values": results.x.n_jets,
            "mask_fn": (lambda v: results.x.n_jets == v),
            "combinations_only": True,
        }
        # store all weights per process id and jet multiplicity
        group_combinations.append(("process", "njet"))
    self[increment_stats](events, results, stats, weight_map, group_map, group_combinations, **kwargs)

    return events, results
