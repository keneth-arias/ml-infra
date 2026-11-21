import unittest

from cpu_admission_receipt import check_image


class CpuAdmissionTest(unittest.TestCase):
    def test_policy_matches_only_qualified_cpu_index(self):
        repository = "ghcr.io/keneth-arias/ml-infra-sky-cpu"
        index = "sha256:" + "a" * 64
        receipt = {"role": "rootless-cpu", "repository": repository, "index": index}
        variable = {"name": "skyImage", "expression": repr(repository + "@" + index)}
        policy = {"spec": {"variables": [variable]}}
        check_image(policy, receipt)
        for field, value in (("role", "trainer"), ("repository", "example.invalid/worker"),
                             ("index", "sha256:" + "b" * 64)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                check_image(policy, dict(receipt, **{field: value}))
        policy["spec"]["variables"].append(variable)
        with self.assertRaises(ValueError):
            check_image(policy, receipt)
        policy["spec"]["variables"] = [dict(variable, expression=repr(repository + "@sha256:" + "f" * 64))]
        with self.assertRaisesRegex(ValueError, "differs"):
            check_image(policy, receipt)
