from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from threading import Event, Thread
from types import SimpleNamespace
import unittest

from protocol import BacktestError, BacktestRequest, Trial
from core.calculation import Calculation
from core.kernel import Kernel
from protocol import DataQuery, Dataset


class SearchSettlement:
    instances = []

    def __init__(self, initial_cash, fees, tax_policy, **params):
        self.initial_cash = initial_cash
        self.score = 0
        self.released = Event()
        type(self).instances.append(self)

    def apply(self, record, following, trade_start):
        self.day = record.trade_date
        return record.frame, SimpleNamespace()

    def plan(self, target, strategy_id):
        self.score = 0 if target is None else target.score

    def fail(self, error):
        return None

    def result(self):
        equity = Decimal("100") + Decimal(str(self.score))
        point = SimpleNamespace(day=self.day, cash=equity, equity=equity)
        return SimpleNamespace(equity=(point,), holdings=(), pending=(), fills=())


class SearchStrategy:
    instances = []

    def __init__(self, parameters):
        self.parameters = parameters
        self.strategy_id = "search-strategy"
        type(self).instances.append(self)

    def requirements(self):
        return SimpleNamespace(fields=("close",), history_bars=self.parameters.get("history", 1), price_basis="adjusted",
                               frequency="daily")

    def generate(self, frame, snapshot):
        return SimpleNamespace(score=self.parameters.get("score", 0))


class SearchData:
    def __init__(self):
        self.queries = []

    def query(self, query):
        self.queries.append(query)
        day = query.start
        record = SimpleNamespace(
            trade_date=day,
            frame=SimpleNamespace(at=datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)),
        )
        return Dataset(
            "fixture", (record,), {"fees": object(), "tax_policy": object()},
            {"source": "test", "calendar_start": day.isoformat()},
            {"universe": "fixture", "symbols": (), "calendar_start": day.isoformat(),
             "market": {"selection": {"scope": "CN_A"}, "execution": "fixture"}},
            (day, day.fromordinal(day.toordinal() + 1)), ("close",), query.history,
        )


def request(**changes):
    value = {
        "run_id": "base", "strategy_id": "search-strategy", "parameters": {},
        "dataset_id": "fixture", "start": datetime(2020, 1, 1, tzinfo=timezone.utc),
        "end": datetime(2020, 1, 1, 23, 59, tzinfo=timezone.utc),
        "initial_cash": Decimal("100"),
    }
    value.update(changes)
    return BacktestRequest(**value)


class ScoreCalculation(Calculation):
    def evaluate(self, points, **parameters):
        return {"score": points[-1][1] - 100}


class KernelSearchTests(unittest.TestCase):
    def setUp(self):
        SearchSettlement.instances = []
        SearchStrategy.instances = []
        self.data = SearchData()
        self.kernel = Kernel(
            self.data,
            lambda _id, params: SearchStrategy(params),
            lambda req, requirements: DataQuery(
                req.dataset_id, req.start.date(), req.end.date(), requirements.fields,
                requirements.history_bars, requirements.price_basis,
                req.as_of or req.end.date(), frequency=requirements.frequency,
                market=req.market,
            ),
            SearchSettlement,
            calculation=ScoreCalculation(),
        )

    def test_atomic_run_is_one_trial_and_search_reuses_the_same_execution_contract(self):
        atomic = self.kernel.run(request(run_id="atomic", parameters={"score": 2}))
        seen = []

        def updater(trials):
            seen.append(trials)
            return {"score": 2}

        searched = self.kernel.search(request(), updater, max_trials=1, metric="score", direction="max")

        self.assertEqual(len(searched.trials), 1)
        self.assertEqual(searched.trials[0].response.final_equity, atomic.final_equity)
        self.assertEqual(searched.trials[0].response.metrics, atomic.metrics)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0], ())

    def test_search_runs_two_independent_trials_and_keeps_first_tie(self):
        seen = []
        candidates = ({"score": 1}, {"score": 3}, {"score": 3})

        def updater(trials):
            seen.append(tuple(trial.request.parameters for trial in trials))
            return candidates[len(trials)]

        result = self.kernel.search(request(), updater, max_trials=3, metric="score", direction="max")

        self.assertEqual([trial.score for trial in result.trials], [1, 3, 3])
        self.assertEqual(result.best_index, 1)
        self.assertEqual([trial.request.run_id for trial in result.trials],
                         ["base-trial-0001", "base-trial-0002", "base-trial-0003"])
        self.assertEqual(seen, [(), ({"score": 1},), ({"score": 1}, {"score": 3})])
        self.assertEqual(len(SearchStrategy.instances), 3)
        self.assertEqual(len(SearchSettlement.instances), 3)

    def test_search_rejects_invalid_score_and_releases_kernel(self):
        class InvalidCalculation(Calculation):
            def evaluate(self, points, **parameters):
                return {"score": None}

        kernel = Kernel(
            self.data, lambda _id, params: SearchStrategy(params),
            self.kernel._build_query, SearchSettlement, calculation=InvalidCalculation(),
        )
        with self.assertRaises(BacktestError) as caught:
            kernel.search(request(), lambda trials: {"score": 1}, max_trials=1, metric="score", direction="max")
        self.assertEqual(caught.exception.code, "search_failed")
        self.assertEqual(caught.exception.completed_trials, ())
        self.assertIsNotNone(caught.exception.partial_result)
        self.assertEqual(kernel.run(request(run_id="after" )).run_id, "after")

    def test_search_failure_keeps_raw_candidate_and_trial_index(self):
        candidates = ({"score": 1}, {"score": []})

        with self.assertRaises(BacktestError) as caught:
            self.kernel.search(
                request(), lambda trials: candidates[len(trials)],
                max_trials=2, metric="score", direction="max",
            )

        self.assertEqual(caught.exception.code, "search_failed")
        self.assertEqual(caught.exception.failed_values, {"score": []})
        self.assertEqual(caught.exception.trial_index, 2)
        self.assertEqual(
            [trial.request.run_id for trial in caught.exception.completed_trials],
            ["base-trial-0001"],
        )

    def test_search_rejects_non_string_direction_as_invalid_search(self):
        with self.assertRaises(BacktestError) as caught:
            self.kernel.search(request(), lambda trials: {}, max_trials=1,
                               metric="score", direction=[])
        self.assertEqual(caught.exception.code, "invalid_search")

    def test_min_direction_keeps_first_lowest_score_and_none_stops_after_a_trial(self):
        candidates = ({"score": 1}, {"score": 3}, {"score": 2})

        result = self.kernel.search(
            request(), lambda trials: candidates[len(trials)] if len(trials) < 2 else None,
            max_trials=3, metric="score", direction="min",
        )

        self.assertEqual([trial.score for trial in result.trials], [1, 3])
        self.assertEqual(result.best_index, 0)

    def test_calculation_propose_copies_candidates_and_rejects_empty_input(self):
        response = self.kernel.run(request(run_id="candidate", parameters={"score": 1}))
        trial = Trial(request(run_id="candidate"), response, 1)
        candidates = [{"score": 1}, {"score": 2}]

        chosen = Calculation.propose((trial,), candidates=candidates)

        self.assertEqual(chosen, {"score": 2})
        self.assertIsNot(chosen, candidates[1])
        with self.assertRaises(ValueError):
            Calculation.propose((), candidates=[])
        with self.assertRaises(ValueError):
            Calculation.propose((), candidates=[{}])
        self.assertIsNone(Calculation.propose((trial, trial), candidates=candidates))

    def test_calculation_propose_validates_only_current_candidate_and_keeps_raw_value(self):
        response = self.kernel.run(request(run_id="candidate-current"))
        trial = Trial(request(run_id="candidate-current"), response, 1)
        candidates = ({"score": 1}, {"score": []})

        self.assertEqual(Calculation.propose((), candidates=candidates), {"score": 1})
        with self.assertRaises(TypeError) as caught:
            Calculation.propose((trial,), candidates=candidates)

        self.assertEqual(caught.exception.failed_values, {"score": []})
        self.assertEqual(caught.exception.trial_index, 2)

    def test_each_trial_requeries_the_requested_history_window(self):
        self.kernel.search(
            request(), lambda trials: {"history": len(trials) + 1} if len(trials) < 2 else None,
            max_trials=2, metric="score", direction="max",
        )

        self.assertEqual([query.history for query in self.data.queries], [1, 2])

    def test_search_rejects_fixed_validation_keyword(self):
        with self.assertRaisesRegex(TypeError, "validation"):
            self.kernel.search(
                request(), lambda trials: {"score": 1}, max_trials=1,
                metric="score", direction="max", validation=request(),
            )

    def test_updater_block_keeps_atomic_run_busy(self):
        entered = Event()
        release = Event()

        def updater(trials):
            entered.set()
            release.wait(2)
            return {"score": 1}

        errors = []

        def search():
            try:
                self.kernel.search(request(), updater, max_trials=1, metric="score", direction="max")
            except Exception as error:
                errors.append(error)

        thread = Thread(target=search)
        thread.start()
        if not entered.wait(2):
            thread.join(2)
            self.fail(f"search did not enter updater: {errors}")
        with self.assertRaises(BacktestError) as caught:
            self.kernel.run(request(run_id="busy"))
        errors.append(caught.exception.code)
        release.set()
        thread.join(2)
        self.assertEqual(errors, ["busy"])


if __name__ == "__main__":
    unittest.main()
