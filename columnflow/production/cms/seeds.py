# coding: utf-8

"""
Methods related to creating event and object seeds.
"""

from __future__ import annotations

import hashlib

import law

from columnflow.production import Producer, producer
from columnflow.util import maybe_import, primes, InsertableDict
from columnflow.columnar_util import Route, set_ak_column, optional_column as optional

np = maybe_import("numpy")
ak = maybe_import("awkward")


logger = law.logger.get_logger(__name__)


@producer(
    uses={
        # global columns for event seed
        "run", "luminosityBlock", "event",
        *optional({
            "Pileup.nPU",
            # columns needed to extract object counts only
            "Photon.pt", "SV.pt", "FatJet.pt", "SubJet.pt",
            "GenJet.pt", "GenPart.pt",
            # per-object columns for further hashing
            "Electron.jetIdx", "Electron.seediPhiOriY",
            "Muon.jetIdx", "Muon.nStations",
            "Tau.jetIdx", "Tau.decayMode",
            "Jet.nConstituents", "Jet.nElectrons", "Jet.nMuons",
        }),
    },
    produces={"deterministic_seed"},
)
def deterministic_event_seeds(self: Producer, events: ak.Array, **kwargs) -> ak.Array:
    """
    Produces deterministic event seeds and stores them in *events* which is also returned.

    Strategy:

        1. gather a selection of unambiguous integer features
        2. multiply them with a vector of primes
        3. use the resulting integer as an input to sha256 and hex-digest the result
        4. reverse it and int-cast the leading 16 characters, leading to a 64 bit int
    """
    # started from an already hashed seed based on event, run and lumi info multiplied with primes
    seed = self.create_seed(
        np.asarray(
            self.primes[7] * ak.values_astype(events.event, np.uint64) +
            self.primes[5] * ak.values_astype(events.run, np.uint64) +
            self.primes[3] * ak.values_astype(events.luminosityBlock, np.uint64),
        ),
        n_hex=14,
    )

    # start gathering global fields when available
    global_fields = []

    # flat fields
    if self.apply_field(events, "Pileup.nPU") is not None:
        global_fields.append("Pileup.nPU")

    # add fields for counts of jagged fields
    collections = ["Jet", "FatJet", "SubJet", "Photon", "Muon", "Electron", "Tau", "SV"]
    if self.dataset_inst.is_mc:
        collections.extend(["GenJet", "GenPart"])
    for col in collections:
        if (arr := self.apply_field(events, col)) is None:
            continue
        global_fields.append(field := f"n{col}")
        events = set_ak_column(events, field, ak.num(arr, axis=1), value_type=np.uint64)

    # calculate seed from global fields
    value_offset = 3
    prime_offset = 15
    for i, f in enumerate(global_fields, value_offset):
        values = Route(f).apply(events) + i
        primes = self.primes[(values + prime_offset) % len(self.primes)]
        seed = seed + primes * ak.values_astype(values, np.uint64)

    # get integers of objects, perform a custom hashing involving local indices,
    # then multiply with primes and add to the seed
    object_fields = [
        "Electron.jetIdx", "Electron.seediPhiOriY", "Tau.jetIdx", "Tau.decayMode", "Muon.jetIdx",
        "Muon.nStations", "Jet.nConstituents", "Jet.nElectrons", "Jet.nMuons",
    ]
    for i, f in enumerate(object_fields, value_offset):
        if (values := self.apply_field(events, f)) is None:
            continue
        values = values + i
        loc = ak.local_index(values) + 1
        hashed = (
            ak.num(values, axis=-1) +
            ak.sum(values * loc, axis=-1) +
            ak.sum(values**2 * loc, axis=-1)
        )
        primes = self.primes[(hashed + prime_offset) % len(self.primes)]
        seed = seed + primes * ak.values_astype(hashed, np.uint64)

    # create and store them
    seed = ak.Array(self.create_seed(np.asarray(seed)))
    events = set_ak_column(events, "deterministic_seed", seed, value_type=np.uint64)

    # uniqueness test across the chunk for debugging
    # n_events = len(seed)
    # n_seeds = len(set(seed))
    # match_text = "yes" if n_events == n_seeds else "NO !!!"
    # print(f"events: {n_events}, unique seeds: {n_seeds}, match: {match_text}")

    return events


@deterministic_event_seeds.setup
def deterministic_event_seeds_setup(
    self: Producer,
    reqs: dict,
    inputs: dict,
    reader_targets: InsertableDict,
) -> None:
    """
    Setup function that defines the vectorized seed creation function once and stores it in the
    py:attr:`create_seed` attribute.
    """
    def create_seed(val: int, n_hex: int = 16) -> int:
        return int(hashlib.sha256(bytes(str(val), "utf-8")).hexdigest()[:-(n_hex + 1):-1], base=16)

    # store a vectorized version (only interface, not actually simd'ing)
    self.create_seed = np.vectorize(create_seed, otypes=[np.uint64])

    # store primes in array
    self.primes = np.array(primes, dtype=np.uint64)

    # helper to apply a field to an array with a silent failure that only issues a warning
    def apply_field(ak_array: ak.Array, field: str) -> ak.Array | None:
        try:
            return Route(field).apply(ak_array)
        except ak.errors.FieldNotFoundError:
            logger.warning_once(
                f"{id(self)}_{field}",
                f"optional field '{field}' not found in events chunk for seed calculation",
            )
            return None

    self.apply_field = apply_field


@producer(
    uses={deterministic_event_seeds},
    produces={"Jet.deterministic_seed"},
)
def deterministic_jet_seeds(self: Producer, events: ak.Array, **kwargs) -> ak.Array:
    """
    Produces deterministic seeds for each jet and stores them in *events* which is also returned.
    The seeds are based on the event seeds produced by :py:func:`deterministic_event_seeds` which is
    also used to access the py:attr:`create_seed` function. The strategy for producing seeds is
    identical.
    """
    # create the event seeds
    events = self[deterministic_event_seeds](events, **kwargs)

    # create the per jet seeds
    primes = self.primes[events.deterministic_seed % len(self.primes)]
    jet_seed = events.deterministic_seed + (
        primes * ak.values_astype(ak.local_index(events.Jet, axis=1) + self.primes[50], np.uint64)
    )
    np_jet_seed = np.asarray(ak.flatten(jet_seed))
    np_jet_seed[:] = self[deterministic_event_seeds].create_seed(np_jet_seed)

    # store them
    events = set_ak_column(events, "Jet.deterministic_seed", jet_seed, value_type=np.uint64)

    # uniqueness test across all jets in the chunk for debugging
    # n_jets = ak.sum(ak.num(events.Jet, axis=1))
    # n_seeds = len(set(np_jet_seed))
    # match_text = "yes" if n_jets == n_seeds else "NO !!!"
    # print(f"jets: {n_jets}, unique seeds: {n_seeds}, match: {match_text}")

    return events


@deterministic_jet_seeds.setup
def deterministic_jet_seeds_setup(
    self: Producer,
    reqs: dict,
    inputs: dict,
    reader_targets: InsertableDict,
) -> None:
    # store primes in array
    self.primes = np.array(primes, dtype=np.uint64)


@producer(
    uses={deterministic_event_seeds, deterministic_jet_seeds},
    produces={deterministic_event_seeds, deterministic_jet_seeds},
)
def deterministic_seeds(self: Producer, events: ak.Array, **kwargs) -> ak.Array:
    """
    Wrapper producer that invokes :py:func:`deterministic_event_seeds` and
    :py:func:`deterministic_jet_seeds`.
    """
    # create the event seeds
    events = self[deterministic_event_seeds](events, **kwargs)

    # create the jet seeds
    events = self[deterministic_jet_seeds](events, **kwargs)

    return events
