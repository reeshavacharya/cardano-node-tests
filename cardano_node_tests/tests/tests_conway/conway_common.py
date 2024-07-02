"""Common functionality for Conway governance tests."""

import dataclasses
from itertools import chain
import json
import logging
import typing as tp

from cardano_clusterlib import clusterlib

from cardano_node_tests.cluster_management import cluster_management
from cardano_node_tests.tests import common
from cardano_node_tests.utils import clusterlib_utils
from cardano_node_tests.utils import governance_setup
from cardano_node_tests.utils import governance_utils

LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class PParamPropRec:
    proposals: tp.List[clusterlib_utils.UpdateProposal]
    action_txid: str
    action_ix: int
    proposal_names: tp.Set[str]
    future_pparams: tp.Dict[str, tp.Any]


def is_in_bootstrap(
    cluster_obj: clusterlib.ClusterLib,
) -> bool:
    """Check if the cluster is in bootstrap period."""
    pv = cluster_obj.g_conway_governance.query.gov_state()["currentPParams"]["protocolVersion"][
        "major"
    ]
    return bool(pv == 9)


def get_committee_val(data: tp.Dict[str, tp.Any]) -> tp.Dict[str, tp.Any]:
    """Get the committee value from the data.

    The key can be either correctly "committee", or with typo "commitee".
    TODO: Remove this function when the typo is fixed in the ledger.
    """
    return data.get("committee") or data.get("commitee") or {}


def possible_rem_issue(gov_state: tp.Dict[str, tp.Any], epoch: int) -> bool:
    """Check if the unexpected removed action situation can be result of known ledger issue.

    When the issue manifests, only single expired action gets removed and all other expired or
    ratified actions are ignored int the given epoch.

    See https://github.com/IntersectMBO/cardano-ledger/issues/3979
    """
    removed_actions: tp.List[tp.Dict[str, tp.Any]] = gov_state["nextRatifyState"][
        "expiredGovActions"
    ]
    proposals: tp.List[tp.Dict[str, tp.Any]] = gov_state["proposals"]

    if len(removed_actions) != 1 or len(proposals) == 1:
        return False

    action_txid = removed_actions[0]["txId"]
    action_ix = removed_actions[0]["govActionIx"]

    for _p in proposals:
        _p_action_id = _p["actionId"]
        if (
            _p["expiresAfter"] < epoch
            and _p_action_id["txId"] == action_txid
            and _p_action_id["govActionIx"] == action_ix
        ):
            return True

    return False


def get_yes_abstain_vote(idx: int) -> clusterlib.Votes:
    """Check that votes of DReps who abstained are not considered as "No" votes."""
    if idx == 1 or idx % 2 == 0:
        return clusterlib.Votes.YES
    if idx % 3 == 0:
        return clusterlib.Votes.NO
    return clusterlib.Votes.ABSTAIN


def get_no_abstain_vote(idx: int) -> clusterlib.Votes:
    """Check that votes of DReps who abstained are not considered as "No" votes."""
    if idx == 1 or idx % 2 == 0:
        return clusterlib.Votes.NO
    if idx % 3 == 0:
        return clusterlib.Votes.YES
    return clusterlib.Votes.ABSTAIN


def save_gov_state(gov_state: tp.Dict[str, tp.Any], name_template: str) -> None:
    """Save governance state to a file."""
    with open(f"{name_template}_gov_state.json", "w", encoding="utf-8") as out_fp:
        json.dump(gov_state, out_fp, indent=2)


def save_committee_state(committee_state: tp.Dict[str, tp.Any], name_template: str) -> None:
    """Save CC state to a file."""
    with open(f"{name_template}_committee_state.json", "w", encoding="utf-8") as out_fp:
        json.dump(committee_state, out_fp, indent=2)


def save_drep_state(drep_state: governance_utils.DRepStateT, name_template: str) -> None:
    """Save DRep state to a file."""
    with open(f"{name_template}_drep_state.json", "w", encoding="utf-8") as out_fp:
        json.dump(drep_state, out_fp, indent=2)


# TODO: move this and reuse in other tests that need a registered stake address.
def get_registered_pool_user(
    cluster_manager: cluster_management.ClusterManager,
    name_template: str,
    cluster_obj: clusterlib.ClusterLib,
    caching_key: str = "",
    fund_amount: int = 1000_000_000,
    no_of_users: int = 1
) -> tp.List[clusterlib.PoolUser]:
    """Create a registered pool user."""

    def _create_user() -> clusterlib.PoolUser:
        pool_users = clusterlib_utils.create_pool_users(
            cluster_obj=cluster_obj,
            name_template=f"{name_template}_pool_user",
            no_of_addr=no_of_users,
        )
        return pool_users

    if caching_key:
        with cluster_manager.cache_fixture(key=caching_key) as fixture_cache:
            if fixture_cache.value:
                return fixture_cache.value  # type: ignore

            pool_users = _create_user()
            fixture_cache.value = pool_users
    else:
        pool_users = _create_user()

    # Fund the payment address with some ADA
    for pool_user in pool_users:
        clusterlib_utils.fund_from_faucet(
            pool_user.payment,
            cluster_obj=cluster_obj,
            faucet_data=cluster_manager.cache.addrs_data["user1"],
            amount=fund_amount,
        )

    # Register the stake address
    stake_deposit_amt = cluster_obj.g_query.get_address_deposit()
    stake_addr_reg_cert = [
        cluster_obj.g_stake_address.gen_stake_addr_registration_cert(
            addr_name=f"{name_template}_pool_user{i}",
            deposit_amt=stake_deposit_amt,
            stake_vkey_file=pool_users[i].stake.vkey_file,
        )
        for i in range(no_of_users)
    ]
    pool_users_payment_skey = [pool_user.payment.skey_file for pool_user in pool_users]
    pool_users_stake_skey = [pool_user.stake.skey_file for pool_user in pool_users]
    tx_files_action = clusterlib.TxFiles(
        certificate_files= stake_addr_reg_cert,
        signing_key_files=pool_users_payment_skey + pool_users_stake_skey,
    )
    clusterlib_utils.build_and_submit_tx(
        cluster_obj=cluster_obj,
        name_template=f"{name_template}_pool_user",
        src_address=pool_users[0].payment.address,
        use_build_cmd=True,
        tx_files=tx_files_action,
    )
    for pool_user in pool_users:
        assert cluster_obj.g_query.get_stake_addr_info(
            pool_user.stake.address
        ).address, f"Stake address is not registered: {pool_user.stake.address}"

    return pool_users


def submit_vote(
    cluster_obj: clusterlib.ClusterLib,
    name_template: str,
    payment_addr: clusterlib.AddressRecord,
    votes: tp.List[governance_utils.VotesAllT],
    keys: tp.List[clusterlib.FileType],
    submit_method: str = "",
    use_build_cmd: bool = False,
) -> clusterlib.TxRawOutput:
    """Submit a Tx with votes."""
    tx_files = clusterlib.TxFiles(
        vote_files=[r.vote_file for r in votes],
        signing_key_files=[
            payment_addr.skey_file,
            *keys,
        ],
    )

    tx_output = clusterlib_utils.build_and_submit_tx(
        cluster_obj=cluster_obj,
        name_template=f"{name_template}_vote",
        src_address=payment_addr.address,
        submit_method=submit_method,
        use_build_cmd=use_build_cmd,
        tx_files=tx_files,
    )

    out_utxos = cluster_obj.g_query.get_utxo(tx_raw_output=tx_output)
    assert (
        clusterlib.filter_utxos(utxos=out_utxos, address=payment_addr.address)[0].amount
        == clusterlib.calculate_utxos_balance(tx_output.txins) - tx_output.fee
    ), f"Incorrect balance for source address `{payment_addr.address}`"

    return tx_output


def cast_vote(
    cluster_obj: clusterlib.ClusterLib,
    governance_data: governance_setup.DefaultGovernance,
    name_template: str,
    payment_addr: clusterlib.AddressRecord,
    action_txid: str,
    action_ix: int,
    approve_cc: tp.Optional[bool] = None,
    approve_drep: tp.Optional[bool] = None,
    approve_spo: tp.Optional[bool] = None,
    cc_skip_votes: bool = False,
    drep_skip_votes: bool = False,
    spo_skip_votes: bool = False,
) -> governance_utils.VotedVotes:
    """Cast a vote."""
    # pylint: disable=too-many-arguments
    votes_cc = []
    votes_drep = []
    votes_spo = []

    if approve_cc is not None:
        _votes_cc = [
            None  # This CC member doesn't vote, his votes count as "No"
            if cc_skip_votes and i % 3 == 0
            else cluster_obj.g_conway_governance.vote.create_committee(
                vote_name=f"{name_template}_cc{i}",
                action_txid=action_txid,
                action_ix=action_ix,
                vote=get_yes_abstain_vote(i) if approve_cc else get_no_abstain_vote(i),
                cc_hot_vkey_file=m.hot_vkey_file,
                anchor_url=f"http://www.cc-vote{i}.com",
                anchor_data_hash="5d372dca1a4cc90d7d16d966c48270e33e3aa0abcb0e78f0d5ca7ff330d2245d",
            )
            for i, m in enumerate(governance_data.cc_members, start=1)
        ]
        votes_cc = [v for v in _votes_cc if v]
    if approve_drep is not None:
        _votes_drep = [
            None  # This DRep doesn't vote, his votes count as "No"
            if drep_skip_votes and i % 3 == 0
            else cluster_obj.g_conway_governance.vote.create_drep(
                vote_name=f"{name_template}_drep{i}",
                action_txid=action_txid,
                action_ix=action_ix,
                vote=get_yes_abstain_vote(i) if approve_drep else get_no_abstain_vote(i),
                drep_vkey_file=d.key_pair.vkey_file,
                anchor_url=f"http://www.drep-vote{i}.com",
                anchor_data_hash="5d372dca1a4cc90d7d16d966c48270e33e3aa0abcb0e78f0d5ca7ff330d2245d",
            )
            for i, d in enumerate(governance_data.dreps_reg, start=1)
        ]
        votes_drep = [v for v in _votes_drep if v]
    if approve_spo is not None:
        _votes_spo = [
            None  # This SPO doesn't vote, his votes count as "No"
            if spo_skip_votes and i % 3 == 0
            else cluster_obj.g_conway_governance.vote.create_spo(
                vote_name=f"{name_template}_pool{i}",
                action_txid=action_txid,
                action_ix=action_ix,
                vote=get_yes_abstain_vote(i) if approve_spo else get_no_abstain_vote(i),
                cold_vkey_file=p.vkey_file,
                anchor_url=f"http://www.spo-vote{i}.com",
                anchor_data_hash="5d372dca1a4cc90d7d16d966c48270e33e3aa0abcb0e78f0d5ca7ff330d2245d",
            )
            for i, p in enumerate(governance_data.pools_cold, start=1)
        ]
        votes_spo = [v for v in _votes_spo if v]

    cc_keys = [r.hot_skey_file for r in governance_data.cc_members] if votes_cc else []
    drep_keys = [r.key_pair.skey_file for r in governance_data.dreps_reg] if votes_drep else []
    spo_keys = [r.skey_file for r in governance_data.pools_cold] if votes_spo else []

    votes_all: tp.List[governance_utils.VotesAllT] = [*votes_cc, *votes_drep, *votes_spo]
    keys_all = [*cc_keys, *drep_keys, *spo_keys]

    # Make sure we have enough time to submit the votes in one epoch
    clusterlib_utils.wait_for_epoch_interval(
        cluster_obj=cluster_obj, start=1, stop=common.EPOCH_STOP_SEC_BUFFER
    )

    submit_vote(
        cluster_obj=cluster_obj,
        name_template=name_template,
        payment_addr=payment_addr,
        votes=votes_all,
        keys=keys_all,
        use_build_cmd=True,
    )

    # Make sure the vote is included in the ledger
    gov_state = cluster_obj.g_conway_governance.query.gov_state()
    _cur_epoch = cluster_obj.g_query.get_epoch()
    save_gov_state(
        gov_state=gov_state,
        name_template=f"{name_template}_vote_{_cur_epoch}",
    )
    prop_vote = governance_utils.lookup_proposal(gov_state=gov_state, action_txid=action_txid)
    assert not votes_cc or prop_vote["committeeVotes"], "No committee votes"
    assert not votes_drep or prop_vote["dRepVotes"], "No DRep votes"
    assert not votes_spo or prop_vote["stakePoolVotes"], "No stake pool votes"

    return governance_utils.VotedVotes(cc=votes_cc, drep=votes_drep, spo=votes_spo)


def resign_ccs(
    cluster_obj: clusterlib.ClusterLib,
    name_template: str,
    ccs_to_resign: tp.List[clusterlib.CCMember],
    payment_addr: clusterlib.AddressRecord,
) -> clusterlib.TxRawOutput:
    """Resign multiple CC Members."""
    res_certs = [
        cluster_obj.g_conway_governance.committee.gen_cold_key_resignation_cert(
            key_name=f"{name_template}_{i}",
            cold_vkey_file=r.cold_vkey_file,
            resignation_metadata_url=f"http://www.cc-resign{i}.com",
            resignation_metadata_hash="5d372dca1a4cc90d7d16d966c48270e33e3aa0abcb0e78f0d5ca7ff330d2245d",
        )
        for i, r in enumerate(ccs_to_resign, start=1)
    ]

    cc_cold_skeys = [r.cold_skey_file for r in ccs_to_resign]
    tx_files = clusterlib.TxFiles(
        certificate_files=res_certs,
        signing_key_files=[payment_addr.skey_file, *cc_cold_skeys],
    )

    tx_output = clusterlib_utils.build_and_submit_tx(
        cluster_obj=cluster_obj,
        name_template=f"{name_template}_res",
        src_address=payment_addr.address,
        use_build_cmd=True,
        tx_files=tx_files,
    )

    cluster_obj.wait_for_new_block(new_blocks=2)
    res_committee_state = cluster_obj.g_conway_governance.query.committee_state()
    save_committee_state(committee_state=res_committee_state, name_template=f"{name_template}_res")
    for cc_member in ccs_to_resign:
        member_key = f"keyHash-{cc_member.cold_vkey_hash}"
        member_rec = res_committee_state["committee"].get(member_key)
        assert (
            not member_rec or member_rec["hotCredsAuthStatus"]["tag"] == "MemberResigned"
        ), "CC Member not resigned"

    return tx_output


def propose_change_constitution(
    cluster_obj: clusterlib.ClusterLib,
    name_template: str,
    anchor_url: str,
    anchor_data_hash: str,
    constitution_url: str,
    constitution_hash: str,
    pool_users: clusterlib.PoolUser,
) -> tp.Tuple[clusterlib.ActionConstitution, str, int]:
    """Propose a constitution change."""
    deposit_amt = cluster_obj.conway_genesis["govActionDeposit"]

    prev_action_rec = governance_utils.get_prev_action(
        action_type=governance_utils.PrevGovActionIds.CONSTITUTION,
        gov_state=cluster_obj.g_conway_governance.query.gov_state(),
    )

    constitution_actions = [
        cluster_obj.g_conway_governance.action.create_constitution(
            action_name=name_template,
            deposit_amt=deposit_amt,
            anchor_url=anchor_url,
            anchor_data_hash=anchor_data_hash,
            constitution_url=constitution_url,
            constitution_hash=constitution_hash,
            prev_action_txid=prev_action_rec.txid,
            prev_action_ix=prev_action_rec.ix,
            deposit_return_stake_vkey_file=pool_user.stake.vkey_file,
        )
        for pool_user in pool_users
    ]

    tx_files = clusterlib.TxFiles(
        proposal_files=[constitution_action.action_file for constitution_action in constitution_actions],
        signing_key_files=[pool_user.payment.skey_file for pool_user in pool_users],
    )

    # Make sure we have enough time to submit the proposal in one epoch
    clusterlib_utils.wait_for_epoch_interval(
        cluster_obj=cluster_obj, start=1, stop=common.EPOCH_STOP_SEC_BUFFER
    )
    address_utxos = [cluster_obj.g_query.get_utxo(pool_user.payment.address) for pool_user in pool_users]
    flatenned_utxos = list(chain.from_iterable(address_utxos))
    tx_output = clusterlib_utils.build_and_submit_tx(
        cluster_obj=cluster_obj,
        name_template=f"{name_template}_constitution_action",
        src_address=pool_users[0].payment.address,
        txins=flatenned_utxos,
        use_build_cmd=True,
        tx_files=tx_files,
    )

    out_utxos = cluster_obj.g_query.get_utxo(tx_raw_output=tx_output)
    assert (
        clusterlib.filter_utxos(utxos=out_utxos, address=pool_users[0].payment.address)[0].amount
        == clusterlib.calculate_utxos_balance(tx_output.txins) - tx_output.fee - deposit_amt
    ), f"Incorrect balance for source address `{pool_users[0].payment.address}`"

    action_txid = cluster_obj.g_transaction.get_txid(tx_body_file=tx_output.out_file)
    action_gov_state = cluster_obj.g_conway_governance.query.gov_state()
    _cur_epoch = cluster_obj.g_query.get_epoch()
    save_gov_state(
        gov_state=action_gov_state,
        name_template=f"{name_template}_constitution_action_{_cur_epoch}",
    )
    prop_action = governance_utils.lookup_proposal(
        gov_state=action_gov_state, action_txid=action_txid
    )
    assert prop_action, "Create constitution action not found"
    assert (
        prop_action["proposalProcedure"]["govAction"]["tag"]
        == governance_utils.ActionTags.NEW_CONSTITUTION.value
    ), "Incorrect action tag"

    action_ix = prop_action["actionId"]["govActionIx"]

    return constitution_actions, action_txid, action_ix


def propose_pparams_update(
    cluster_obj: clusterlib.ClusterLib,
    name_template: str,
    anchor_url: str,
    anchor_data_hash: str,
    pool_users: clusterlib.PoolUser,
    proposals: tp.List[clusterlib_utils.UpdateProposal],
    prev_action_rec: tp.Optional[governance_utils.PrevActionRec] = None,
) -> PParamPropRec:
    """Propose a pparams update."""
    deposit_amt = cluster_obj.conway_genesis["govActionDeposit"]

    prev_action_rec = prev_action_rec or governance_utils.get_prev_action(
        action_type=governance_utils.PrevGovActionIds.PPARAM_UPDATE,
        gov_state=cluster_obj.g_conway_governance.query.gov_state(),
    )

    update_args = clusterlib_utils.get_pparams_update_args(update_proposals=proposals)
    pparams_actions = [
        cluster_obj.g_conway_governance.action.create_pparams_update(
            action_name=name_template,
            deposit_amt=deposit_amt,
            anchor_url=anchor_url,
            anchor_data_hash=anchor_data_hash,
            cli_args=update_args,
            prev_action_txid=prev_action_rec.txid,
            prev_action_ix=prev_action_rec.ix,
            deposit_return_stake_vkey_file=pool_user.stake.vkey_file,
        )
        for pool_user in pool_users
    ]

    tx_files_action = clusterlib.TxFiles(
        proposal_files=[pparams_action.action_file for pparams_action in pparams_actions],
        signing_key_files=[pool_user.payment.skey_file for pool_user in pool_users],
    )

    # Make sure we have enough time to submit the proposal in one epoch
    clusterlib_utils.wait_for_epoch_interval(
        cluster_obj=cluster_obj, start=1, stop=common.EPOCH_STOP_SEC_BUFFER
    )

    tx_output_action = clusterlib_utils.build_and_submit_tx(
        cluster_obj=cluster_obj,
        name_template=f"{name_template}_action",
        src_address=pool_users[0].payment.address,
        use_build_cmd=True,
        tx_files=tx_files_action,
    )

    out_utxos_action = cluster_obj.g_query.get_utxo(tx_raw_output=tx_output_action)
    assert (
        clusterlib.filter_utxos(utxos=out_utxos_action, address=pool_users[0].payment.address)[0].amount
        == clusterlib.calculate_utxos_balance(tx_output_action.txins)
        - tx_output_action.fee
        - deposit_amt
    ), f"Incorrect balance for source address `{pool_users[0].payment.address}`"

    action_txid = cluster_obj.g_transaction.get_txid(tx_body_file=tx_output_action.out_file)
    action_gov_state = cluster_obj.g_conway_governance.query.gov_state()
    _cur_epoch = cluster_obj.g_query.get_epoch()
    save_gov_state(gov_state=action_gov_state, name_template=f"{name_template}_action_{_cur_epoch}")
    prop_action = governance_utils.lookup_proposal(
        gov_state=action_gov_state, action_txid=action_txid
    )
    assert prop_action, "Param update action not found"
    assert (
        prop_action["proposalProcedure"]["govAction"]["tag"]
        == governance_utils.ActionTags.PARAMETER_CHANGE.value
    ), "Incorrect action tag"

    action_ix = prop_action["actionId"]["govActionIx"]
    proposal_names = {p.name for p in proposals}

    return PParamPropRec(
        proposals=proposals,
        action_txid=action_txid,
        action_ix=action_ix,
        proposal_names=proposal_names,
        future_pparams=prop_action["proposalProcedure"]["govAction"]["contents"][1],
    )
