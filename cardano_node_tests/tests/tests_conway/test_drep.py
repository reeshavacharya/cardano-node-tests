"""Tests for Conway governance DRep functionality."""
import logging
import pathlib as pl

import allure
import pytest
from _pytest.fixtures import FixtureRequest
from cardano_clusterlib import clusterlib

from cardano_node_tests.cluster_management import cluster_management
from cardano_node_tests.tests import common
from cardano_node_tests.utils import cluster_nodes
from cardano_node_tests.utils import clusterlib_utils
from cardano_node_tests.utils import helpers
from cardano_node_tests.utils import submit_utils
from cardano_node_tests.utils.versions import VERSIONS

LOGGER = logging.getLogger(__name__)
DATA_DIR = pl.Path(__file__).parent / "data"

pytestmark = pytest.mark.skipif(
    VERSIONS.transaction_era < VERSIONS.CONWAY,
    reason="runs only with Tx era >= Conway",
)


@pytest.fixture
def payment_addr(
    cluster_manager: cluster_management.ClusterManager,
    cluster: clusterlib.ClusterLib,
) -> clusterlib.AddressRecord:
    """Create new payment address."""
    with cluster_manager.cache_fixture() as fixture_cache:
        if fixture_cache.value:
            return fixture_cache.value  # type: ignore

        addr = clusterlib_utils.create_payment_addr_records(
            f"drep_addr_ci{cluster_manager.cluster_instance_num}",
            cluster_obj=cluster,
        )[0]
        fixture_cache.value = addr

    # Fund source address
    clusterlib_utils.fund_from_faucet(
        addr,
        cluster_obj=cluster,
        faucet_data=cluster_manager.cache.addrs_data["user1"],
    )

    return addr


@pytest.fixture
def pool_user(
    cluster_manager: cluster_management.ClusterManager,
    cluster: clusterlib.ClusterLib,
) -> clusterlib.PoolUser:
    """Create a pool user."""
    test_id = common.get_test_id(cluster)
    pool_user = clusterlib_utils.create_pool_users(
        cluster_obj=cluster,
        name_template=f"{test_id}_pool_user",
        no_of_addr=1,
    )[0]

    # Fund the payment address with some ADA
    clusterlib_utils.fund_from_faucet(
        pool_user.payment,
        cluster_obj=cluster,
        faucet_data=cluster_manager.cache.addrs_data["user1"],
        amount=1_500_000,
    )
    return pool_user


@pytest.fixture
def custom_drep(
    cluster_manager: cluster_management.ClusterManager,
    cluster: clusterlib.ClusterLib,
    payment_addr: clusterlib.AddressRecord,
) -> clusterlib_utils.DRepRegistration:
    """Create a custom DRep."""
    if cluster_nodes.get_cluster_type().type != cluster_nodes.ClusterType.LOCAL:
        pytest.skip("runs only on local cluster")

    with cluster_manager.cache_fixture() as fixture_cache:
        if fixture_cache.value:
            return fixture_cache.value  # type: ignore

        reg_drep = clusterlib_utils.register_drep(
            cluster_obj=cluster,
            name_template="drep_custom",
            payment_addr=payment_addr,
            submit_method=submit_utils.SubmitMethods.CLI,
            use_build_cmd=True,
        )
        fixture_cache.value = reg_drep

    return reg_drep


def _check_delegation(deleg_state: dict, drep_id: str, stake_addr_hash: str) -> None:
    drep_records = deleg_state["dstate"]["unified"]["credentials"]

    stake_addr_key = f"keyHash-{stake_addr_hash}"
    stake_addr_val = drep_records.get(stake_addr_key) or {}

    expected_drep = f"drep-keyHash-{drep_id}"
    if drep_id == "always_abstain":
        expected_drep = "drep-alwaysAbstain"
    elif drep_id == "always_no_confidence":
        expected_drep = "drep-alwaysNoConfidence"

    assert stake_addr_val.get("drep") == expected_drep


class TestDReps:
    """Tests for DReps."""

    @allure.link(helpers.get_vcs_link())
    @submit_utils.PARAM_SUBMIT_METHOD
    @common.PARAM_USE_BUILD_CMD
    def test_register_and_retire_drep(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addr: clusterlib.AddressRecord,
        use_build_cmd: bool,
        submit_method: str,
    ):
        """Test DRep registration and retirement.

        * register DRep
        * check that DRep was registered
        * retire DRep
        * check that DRep was retired
        * check that deposit was returned to source address
        """
        temp_template = common.get_test_id(cluster)

        # Register DRep

        reg_drep = clusterlib_utils.register_drep(
            cluster_obj=cluster,
            name_template=temp_template,
            payment_addr=payment_addr,
            submit_method=submit_method,
            use_build_cmd=use_build_cmd,
        )

        reg_out_utxos = cluster.g_query.get_utxo(tx_raw_output=reg_drep.tx_output)
        assert (
            clusterlib.filter_utxos(utxos=reg_out_utxos, address=payment_addr.address)[0].amount
            == clusterlib.calculate_utxos_balance(reg_drep.tx_output.txins)
            - reg_drep.tx_output.fee
            - reg_drep.deposit
        ), f"Incorrect balance for source address `{payment_addr.address}`"

        reg_drep_state = cluster.g_conway_governance.query.drep_state(
            drep_vkey_file=reg_drep.key_pair.vkey_file
        )
        assert reg_drep_state[0][0]["keyHash"] == reg_drep.drep_id, "DRep was not registered"

        # Retire DRep

        ret_cert = cluster.g_conway_governance.drep.gen_retirement_cert(
            cert_name=temp_template,
            deposit_amt=reg_drep.deposit,
            drep_vkey_file=reg_drep.key_pair.vkey_file,
        )

        tx_files_ret = clusterlib.TxFiles(
            certificate_files=[ret_cert],
            signing_key_files=[payment_addr.skey_file, reg_drep.key_pair.skey_file],
        )

        if use_build_cmd:
            tx_output_ret = cluster.g_transaction.build_tx(
                src_address=payment_addr.address,
                tx_name=temp_template,
                tx_files=tx_files_ret,
                witness_override=len(tx_files_ret.signing_key_files),
            )
        else:
            fee_ret = cluster.g_transaction.calculate_tx_fee(
                src_address=payment_addr.address,
                tx_name=temp_template,
                tx_files=tx_files_ret,
                witness_count_add=len(tx_files_ret.signing_key_files),
            )
            tx_output_ret = cluster.g_transaction.build_raw_tx(
                src_address=payment_addr.address,
                tx_name=temp_template,
                tx_files=tx_files_ret,
                fee=fee_ret,
                deposit=-reg_drep.deposit,
            )

        tx_signed_ret = cluster.g_transaction.sign_tx(
            tx_body_file=tx_output_ret.out_file,
            signing_key_files=tx_files_ret.signing_key_files,
            tx_name=temp_template,
        )
        submit_utils.submit_tx(
            submit_method=submit_method,
            cluster_obj=cluster,
            tx_file=tx_signed_ret,
            txins=tx_output_ret.txins,
        )

        ret_drep_state = cluster.g_conway_governance.query.drep_state(
            drep_vkey_file=reg_drep.key_pair.vkey_file
        )
        assert not ret_drep_state, "DRep was not retired"

        ret_out_utxos = cluster.g_query.get_utxo(tx_raw_output=reg_drep.tx_output)
        assert (
            clusterlib.filter_utxos(utxos=ret_out_utxos, address=payment_addr.address)[0].amount
            == clusterlib.calculate_utxos_balance(tx_output_ret.txins)
            - tx_output_ret.fee
            + reg_drep.deposit
        ), f"Incorrect balance for source address `{payment_addr.address}`"


class TestDelegDReps:
    """Tests for votes delegation to DReps."""

    @allure.link(helpers.get_vcs_link())
    @submit_utils.PARAM_SUBMIT_METHOD
    @common.PARAM_USE_BUILD_CMD
    @pytest.mark.parametrize("drep", ("always_abstain", "always_no_confidence", "custom"))
    @pytest.mark.testnets
    @pytest.mark.smoke
    def test_dreps_delegation(
        self,
        cluster: clusterlib.ClusterLib,
        payment_addr: clusterlib.AddressRecord,
        pool_user: clusterlib.PoolUser,
        custom_drep: clusterlib_utils.DRepRegistration,
        testfile_temp_dir: pl.Path,
        request: FixtureRequest,
        use_build_cmd: bool,
        submit_method: str,
        drep: str,
    ):
        """Test delegating to DReps.

        * register stake address
        * delegate stake to following DReps:

            - always-abstain
            - always-no-confidence
            - custom DRep

        * check that stake address is registered
        """
        temp_template = common.get_test_id(cluster)
        deposit_amt = cluster.g_query.get_address_deposit()

        # Create stake address registration cert
        reg_cert = cluster.g_stake_address.gen_stake_addr_registration_cert(
            addr_name=f"{temp_template}_addr0",
            deposit_amt=deposit_amt,
            stake_vkey_file=pool_user.stake.vkey_file,
        )

        # Create vote delegation cert
        deleg_cert = cluster.g_stake_address.gen_vote_delegation_cert(
            addr_name=temp_template,
            stake_vkey_file=pool_user.stake.vkey_file,
            drep_key_hash=custom_drep.drep_id if drep == "custom" else "",
            always_abstain=drep == "always_abstain",
            always_no_confidence=drep == "always_no_confidence",
        )

        tx_files = clusterlib.TxFiles(
            certificate_files=[reg_cert, deleg_cert],
            signing_key_files=[payment_addr.skey_file, pool_user.stake.skey_file],
        )

        if use_build_cmd:
            tx_output = cluster.g_transaction.build_tx(
                src_address=payment_addr.address,
                tx_name=temp_template,
                tx_files=tx_files,
                witness_override=len(tx_files.signing_key_files),
            )
        else:
            fee = cluster.g_transaction.calculate_tx_fee(
                src_address=payment_addr.address,
                tx_name=temp_template,
                tx_files=tx_files,
                witness_count_add=len(tx_files.signing_key_files),
            )
            tx_output = cluster.g_transaction.build_raw_tx(
                src_address=payment_addr.address,
                tx_name=temp_template,
                tx_files=tx_files,
                fee=fee,
                deposit=deposit_amt,
            )

        tx_signed = cluster.g_transaction.sign_tx(
            tx_body_file=tx_output.out_file,
            signing_key_files=tx_files.signing_key_files,
            tx_name=temp_template,
        )

        # Make sure we have enough time to finish the registration/delegation in one epoch
        clusterlib_utils.wait_for_epoch_interval(
            cluster_obj=cluster, start=1, stop=common.EPOCH_STOP_SEC_LEDGER_STATE
        )

        submit_utils.submit_tx(
            submit_method=submit_method,
            cluster_obj=cluster,
            tx_file=tx_signed,
            txins=tx_output.txins,
        )

        # Deregister stake address so it doesn't affect stake distribution
        def _deregister():
            with helpers.change_cwd(testfile_temp_dir):
                # Deregister stake address
                stake_addr_dereg_cert = cluster.g_stake_address.gen_stake_addr_deregistration_cert(
                    addr_name=f"{temp_template}_addr0",
                    deposit_amt=deposit_amt,
                    stake_vkey_file=pool_user.stake.vkey_file,
                )
                tx_files_dereg = clusterlib.TxFiles(
                    certificate_files=[stake_addr_dereg_cert],
                    signing_key_files=[
                        payment_addr.skey_file,
                        pool_user.stake.skey_file,
                    ],
                )
                cluster.g_transaction.send_tx(
                    src_address=payment_addr.address,
                    tx_name=f"{temp_template}_dereg",
                    tx_files=tx_files_dereg,
                    deposit=-deposit_amt,
                )

        request.addfinalizer(_deregister)

        assert cluster.g_query.get_stake_addr_info(
            pool_user.stake.address
        ).address, f"Stake address is NOT registered: {pool_user.stake.address}"

        out_utxos = cluster.g_query.get_utxo(tx_raw_output=tx_output)
        assert (
            clusterlib.filter_utxos(utxos=out_utxos, address=payment_addr.address)[0].amount
            == clusterlib.calculate_utxos_balance(tx_output.txins) - tx_output.fee - deposit_amt
        ), f"Incorrect balance for source address `{payment_addr.address}`"

        # Check that stake address is delegated to the correct DRep.
        # This takes one epoch, so test this only for selected combinations of build command
        # and submit method, only when we are running on local testnet, and only if we are not
        # running smoke tests.
        if (
            use_build_cmd
            and submit_method == submit_utils.SubmitMethods.CLI
            and cluster_nodes.get_cluster_type().type == cluster_nodes.ClusterType.LOCAL
            and "smoke" not in request.config.getoption("-m")
        ):
            cluster.wait_for_new_epoch(padding_seconds=5)
            deleg_state = clusterlib_utils.get_delegation_state(cluster_obj=cluster)
            drep_id = custom_drep.drep_id if drep == "custom" else drep
            stake_addr_hash = cluster.g_stake_address.get_stake_vkey_hash(
                stake_vkey_file=pool_user.stake.vkey_file
            )
            _check_delegation(
                deleg_state=deleg_state, drep_id=drep_id, stake_addr_hash=stake_addr_hash
            )