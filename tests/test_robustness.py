"""Robustness plumbing: --config / --out must not rewrite canonical data/."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from eval.build_sensitivity_configs import DELTA, _shift
from eval.metrics import identity_mismatches, load_world
from generator.config import CONFIG_PATH, load_failure_classes
from generator.generate import generate
from generator.presence import _validate


ROOT = Path(__file__).resolve().parents[1]
CALIBRATED = ROOT / "config" / "failure_classes_calibrated.csv"
CANONICAL_FILES = (
    "payments_visible.csv",
    "customers_visible.csv",
    "payments_hidden.csv",
    "customers_latent.csv",
    "ground_truth.csv",
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CalibratedConfigTests(unittest.TestCase):
    def test_config_loads_and_weights_sum_to_one(self):
        classes = load_failure_classes(CALIBRATED)
        total = sum(fc.gen_weight for fc in classes.values())
        self.assertAlmostEqual(total, 1.0, places=6)
        self.assertEqual(classes["technical_downtime"].gen_weight, 0.18)
        self.assertEqual(classes["insufficient_funds"].gen_weight, 0.28)

    def test_p_resolves_matches_canonical(self):
        # Calibration is weights only. Mixing a p_resolves edit here would
        # make the sensitivity sweep uninterpretable.
        calibrated = load_failure_classes(CALIBRATED)
        canonical = load_failure_classes(CONFIG_PATH)
        self.assertEqual(set(calibrated), set(canonical))
        for cid in canonical:
            self.assertEqual(
                calibrated[cid].p_resolves, canonical[cid].p_resolves, cid)
            self.assertEqual(
                calibrated[cid].max_attempts, canonical[cid].max_attempts, cid)


class GenerateOutTests(unittest.TestCase):
    def test_out_leaves_canonical_data_untouched(self):
        before = {name: _digest(ROOT / "data" / name) for name in CANONICAL_FILES}
        tmp = Path(tempfile.mkdtemp(prefix="robust_out_"))
        try:
            generate(
                60, seed=99, out_dir=tmp, config_path=CALIBRATED,
            )
            after = {name: _digest(ROOT / "data" / name) for name in CANONICAL_FILES}
            self.assertEqual(before, after)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_default_generate_reproduces_canonical_batch(self):
        # No-flag generate (canonical CSV, uniform hours, 0.35/0.45) must
        # still match data/. If this fails, a robustness default leaked.
        tmp = Path(tempfile.mkdtemp(prefix="robust_canon_"))
        try:
            generate(1000, seed=42, out_dir=tmp)
            for name in CANONICAL_FILES:
                self.assertEqual(
                    (tmp / name).read_bytes(),
                    (ROOT / "data" / name).read_bytes(),
                    name,
                )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_identity_on_small_calibrated_batch(self):
        tmp = Path(tempfile.mkdtemp(prefix="robust_ident_"))
        try:
            generate(80, seed=7, out_dir=tmp, config_path=CALIBRATED)
            world = load_world(tmp)
            self.assertEqual(len(world["pay_vis"]), 80)
            self.assertEqual(identity_mismatches(world), 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class PresenceStillValidTests(unittest.TestCase):
    def test_validate_import_clean(self):
        _validate()


class SensitivityConfigTests(unittest.TestCase):
    def test_shift_clamps_and_leaves_weights(self):
        tmp = Path(tempfile.mkdtemp(prefix="sens_cfg_"))
        try:
            pes = tmp / "pes.csv"
            opt = tmp / "opt.csv"
            _shift(pes, -DELTA)
            _shift(opt, +DELTA)
            canon = load_failure_classes(CONFIG_PATH)
            pessimistic = load_failure_classes(pes)
            optimistic = load_failure_classes(opt)
            for cid, fc in canon.items():
                self.assertEqual(pessimistic[cid].gen_weight, fc.gen_weight, cid)
                self.assertEqual(optimistic[cid].gen_weight, fc.gen_weight, cid)
                self.assertAlmostEqual(
                    pessimistic[cid].p_resolves,
                    max(0.0, min(1.0, fc.p_resolves - DELTA)),
                    places=6, msg=cid)
                self.assertAlmostEqual(
                    optimistic[cid].p_resolves,
                    max(0.0, min(1.0, fc.p_resolves + DELTA)),
                    places=6, msg=cid)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
