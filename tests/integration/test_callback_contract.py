"""Real Odoo 19 integration test for cursor callback snapshotting, deep mutation, and exact restoration."""
from copy import deepcopy
import pytest
from modootest.adapter.v19.transaction import Odoo19Adapter
from modootest.isolation.transaction import TestTransaction


def _base_precommit_1():
    pass


def _base_precommit_2():
    pass


def _base_postcommit_1():
    pass


def _base_postcommit_2():
    pass


def _base_prerollback_1():
    pass


def _base_prerollback_2():
    pass


def _base_postrollback_1():
    pass


def _base_postrollback_2():
    pass


def _mutated_precommit():
    pass


def _mutated_postcommit():
    pass


def _mutated_prerollback():
    pass


def _mutated_postrollback():
    pass


class SeedingCallbackAdapter(Odoo19Adapter):
    """Test-only adapter subclass that seeds baseline callbacks before snapshotting and observes before-close state."""

    def __init__(self, registry):
        super().__init__(registry)
        self.cursor_instance = None
        self.observed_count = 0
        self.expected_baseline = {}

    def acquire_cursor(self, registry):
        cr = super().acquire_cursor(registry)
        self.cursor_instance = cr

        # Seed harmless baseline callbacks and nested data BEFORE snapshotting
        cr.precommit.add(_base_precommit_1)
        cr.precommit.add(_base_precommit_2)
        cr.precommit.data["nested"] = {"k1": [1, 2], "sub": {"flag": True}}

        cr.postcommit.add(_base_postcommit_1)
        cr.postcommit.add(_base_postcommit_2)
        cr.postcommit.data["nested"] = {"p1": [10, 20], "meta": {"owner": "base"}}

        cr.prerollback.add(_base_prerollback_1)
        cr.prerollback.add(_base_prerollback_2)
        cr.prerollback.data["nested"] = {"r1": "pre_roll", "depth": {"level": 1}}

        cr.postrollback.add(_base_postrollback_1)
        cr.postrollback.add(_base_postrollback_2)
        cr.postrollback.data["nested"] = {"r2": "post_roll", "items": ["a", "b"]}

        # Retain expected baseline copies for comparison
        self.expected_baseline = {
            "precommit_funcs": list(cr.precommit._funcs),
            "precommit_data": deepcopy(cr.precommit.data),
            "postcommit_funcs": list(cr.postcommit._funcs),
            "postcommit_data": deepcopy(cr.postcommit.data),
            "prerollback_funcs": list(cr.prerollback._funcs),
            "prerollback_data": deepcopy(cr.prerollback.data),
            "postrollback_funcs": list(cr.postrollback._funcs),
            "postrollback_data": deepcopy(cr.postrollback.data),
        }
        return cr

    def close_cursor(self, cr):
        try:
            self.observed_count += 1

            # 1. Compare exact function identity and addition order for all 4 queues
            assert list(cr.precommit._funcs) == self.expected_baseline["precommit_funcs"], (
                "precommit _funcs must match baseline function identity and order"
            )
            assert list(cr.postcommit._funcs) == self.expected_baseline["postcommit_funcs"], (
                "postcommit _funcs must match baseline function identity and order"
            )
            assert list(cr.prerollback._funcs) == self.expected_baseline["prerollback_funcs"], (
                "prerollback _funcs must match baseline function identity and order"
            )
            assert list(cr.postrollback._funcs) == self.expected_baseline["postrollback_funcs"], (
                "postrollback _funcs must match baseline function identity and order"
            )

            # 2. Compare deep nested callback data for all 4 queues
            assert cr.precommit.data == self.expected_baseline["precommit_data"], (
                "precommit data must match baseline deep nested data"
            )
            assert cr.postcommit.data == self.expected_baseline["postcommit_data"], (
                "postcommit data must match baseline deep nested data"
            )
            assert cr.prerollback.data == self.expected_baseline["prerollback_data"], (
                "prerollback data must match baseline deep nested data"
            )
            assert cr.postrollback.data == self.expected_baseline["postrollback_data"], (
                "postrollback data must match baseline deep nested data"
            )
        finally:
            super().close_cursor(cr)


@pytest.mark.parametrize("should_raise", [False, True], ids=["success_path", "body_exception_path"])
def test_odoo19_cursor_callbacks_exact_restoration(odoo_registry, should_raise):
    """
    Verify on real Odoo 19 that all 4 callback queues (precommit, postcommit, prerollback, postrollback)
    and their deep nested data are restored exactly to their pre-test baseline before cursor close,
    on both successful and body-exception exit paths.
    """
    adapter = SeedingCallbackAdapter(odoo_registry)

    def _mutate_queues_and_data(cr):
        # 1. Mutate precommit
        assert len(cr.precommit._funcs) == 2
        cr.precommit._funcs.popleft()
        cr.precommit.add(_mutated_precommit)
        cr.precommit.data["nested"]["k1"].append(999)
        cr.precommit.data["new_precommit_key"] = "added_value"
        del cr.precommit.data["nested"]["sub"]

        # 2. Mutate postcommit
        assert len(cr.postcommit._funcs) == 2
        cr.postcommit._funcs.clear()
        cr.postcommit.add(_mutated_postcommit)
        cr.postcommit.data["nested"]["p1"].extend([30, 40])
        cr.postcommit.data["extra_postcommit_key"] = {"x": 1}
        del cr.postcommit.data["nested"]["meta"]

        # 3. Mutate prerollback
        assert len(cr.prerollback._funcs) == 2
        cr.prerollback._funcs.pop()
        cr.prerollback.add(_mutated_prerollback)
        cr.prerollback.data["nested"]["depth"]["level"] = 99
        cr.prerollback.data["preroll_added"] = True
        del cr.prerollback.data["nested"]["r1"]

        # 4. Mutate postrollback
        assert len(cr.postrollback._funcs) == 2
        cr.postrollback._funcs.popleft()
        cr.postrollback.add(_mutated_postrollback)
        cr.postrollback.data["nested"]["items"].clear()
        cr.postrollback.data["postroll_extra"] = 123
        del cr.postrollback.data["nested"]["r2"]

    if should_raise:
        with pytest.raises(ValueError, match="Intentional body exception"):
            with TestTransaction(odoo_registry, adapter=adapter) as tx:
                _mutate_queues_and_data(tx.cr)
                raise ValueError("Intentional body exception")
    else:
        with TestTransaction(odoo_registry, adapter=adapter) as tx:
            _mutate_queues_and_data(tx.cr)

    # Observer assertions at BEFORE-close boundary must have run exactly once
    assert adapter.observed_count == 1, "close_cursor observer must have executed exactly once"

    # Actual Odoo cursor must be closed after transaction exit
    assert adapter.cursor_instance is not None
    assert adapter.cursor_instance.closed is True, "Actual Odoo cursor must be closed"
