"""Guard the private-branch sample budget against regression to cheap guessing."""
import re
import unittest

from verifier.functional import _public_workloads, _scenario


class ProgrammableCoverageTests(unittest.TestCase):
    def test_default_suite_has_32_programmable_cases_and_75_required_stages(self):
        requests = _public_workloads(20260928)
        programs = [request for request in requests if request['kind'] == 'programmable']
        self.assertEqual(len(requests), 43)
        self.assertEqual(len(programs), 32)
        self.assertTrue(all(len(request['programs']) == 2 for request in programs))
        self.assertEqual(sum(2 if request['kind'] == 'programmable' else 1 for request in requests), 75)

    def test_each_programmable_case_samples_a_new_private_first_branch(self):
        class ObservedRandom:
            calls = 0
            def randrange(self, size):
                self.calls += 1
                if size != 2:
                    raise AssertionError('expected a binary branch draw')
                return self.calls % 2
            def randint(self, low, high):
                return low
        rng = ObservedRandom()
        programs = [request for request in _public_workloads(123) if request['kind'] == 'programmable']
        for index, request in enumerate(programs):
            stages = [{'setup': [], 'start': [], 'read': []} for _ in range(2)]
            scenario, _ = _scenario(index, request, stages, rng)
            branches = list(map(int, re.findall(r'peer_value\[1\]=(\d)', scenario)))
            self.assertEqual(branches, [(index+1) % 2, index % 2])
        self.assertEqual(rng.calls, 32)


if __name__ == '__main__':
    unittest.main()
