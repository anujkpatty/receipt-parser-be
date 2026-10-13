import json
from datetime import date

import pytest

import expense_agent
import storage
from models import ReceiptSummary


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeModels:
    """Categorizes by keyword; the agent call returns a fixed summary without tool calls."""
    KEYWORDS = {"beer": "alcohol", "wine": "alcohol", "martini": "alcohol",
                "burger": "food", "soda": "beverages"}

    def generate_content(self, model, contents, config=None):
        if config is not None and config.response_schema is not None:
            lines = contents.split("Items:")[1].strip().splitlines()
            out = []
            for line in lines:
                idx, name = line.strip().split(": ", 1)
                cat = next((c for k, c in self.KEYWORDS.items() if k in name.lower()), "other")
                out.append({"index": int(idx), "category": cat})
            return FakeResponse(json.dumps({"items": out}))
        return FakeResponse("Looks normal.")


class FakeClient:
    models = FakeModels()


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "test.db"))
    storage.init_db()


def make_receipt(day, items):
    return ReceiptSummary(
        store_name="Bar", store_address="1 Main St", store_number=None,
        items=[{"name": n, "price": p} for n, p in items],
        tax=None, total=sum(p or 0 for _, p in items), date=day, payment_method="credit",
    )


def test_period_window():
    assert expense_agent.period_window("week", date(2026, 10, 15)) == ("2026-10-12", "2026-10-18")
    assert expense_agent.period_window("month", date(2026, 2, 10)) == ("2026-02-01", "2026-02-28")
    assert expense_agent.period_window("day", date(2026, 2, 10)) == ("2026-02-10", "2026-02-10")


def test_categorizes_and_stores():
    result = expense_agent.analyze_receipt(
        FakeClient(), "u1", make_receipt("2026-10-13", [("Beer", 8.0), ("Burger", 15.0), ("Mystery", None)])
    )
    assert [i["category"] for i in result["items"]] == ["alcohol", "food"]
    assert result["flags"] == []
    assert result["summary"] == "Looks normal."


def test_weekly_category_rule_flags_when_crossed_and_stays_over():
    storage.add_rule("u1", "alcohol", "week", 50)
    r1 = expense_agent.analyze_receipt(FakeClient(), "u1", make_receipt("2026-10-12", [("Wine", 40.0)]))
    assert r1["flags"] == []

    r2 = expense_agent.analyze_receipt(FakeClient(), "u1", make_receipt("2026-10-14", [("Beer", 20.0), ("Burger", 30.0)]))
    assert len(r2["flags"]) == 1
    assert "crossed" in r2["flags"][0]["message"]
    assert r2["flags"][0]["severity"] == "warning"

    r3 = expense_agent.analyze_receipt(FakeClient(), "u1", make_receipt("2026-10-15", [("Martini", 20.0)]))
    assert "still over" in r3["flags"][0]["message"]
    assert r3["flags"][0]["severity"] == "critical"  # $80 >= 1.5 * $50

    # A receipt with no alcohol doesn't re-trigger the alcohol rule.
    r4 = expense_agent.analyze_receipt(FakeClient(), "u1", make_receipt("2026-10-16", [("Burger", 20.0)]))
    assert r4["flags"] == []

    # Next week starts fresh.
    r5 = expense_agent.analyze_receipt(FakeClient(), "u1", make_receipt("2026-10-19", [("Beer", 10.0)]))
    assert r5["flags"] == []


def test_total_and_per_receipt_rules():
    storage.add_rule("u1", None, "month", 100)
    storage.add_rule("u1", "food", "receipt", 25)
    result = expense_agent.analyze_receipt(
        FakeClient(), "u1", make_receipt("2026-10-13", [("Burger", 30.0), ("Beer", 80.0)])
    )
    messages = sorted(f["message"] for f in result["flags"])
    assert len(messages) == 2
    assert messages[0].startswith("Food spending on this receipt")
    assert messages[1].startswith("Total spending this month")


def test_rules_are_per_user():
    storage.add_rule("u1", "alcohol", "receipt", 5)
    result = expense_agent.analyze_receipt(FakeClient(), "u2", make_receipt("2026-10-13", [("Beer", 50.0)]))
    assert result["flags"] == []


def make_tools(day, rule_flags=(), raised=None):
    raised = [] if raised is None else raised
    tools = expense_agent.make_agent_tools("u1", 1, day, list(rule_flags), raised)
    return {f.__name__: f for f in tools}, raised


def test_agent_flag_tool_guards():
    for d in ("2026-09-22", "2026-09-29", "2026-10-06", "2026-10-13"):
        storage.save_receipt("u1", "Bar", d, 10, [("Beer", 10.0, "alcohol")])
    tools, raised = make_tools(date(2026, 10, 13), rule_flags=[{"category": "alcohol"}])

    assert "error" in tools["flag_expense"]("warning", "alcohol", "dup of rule flag")
    assert "error" in tools["flag_expense"]("urgent", "food", "bad severity")
    assert "error" in tools["flag_expense"]("warning", "snacks", "bad category")
    assert tools["flag_expense"]("info", "total", "Spend is high")["status"] == "flagged"
    assert "error" in tools["flag_expense"]("info", "total", "again")  # once per category
    assert tools["flag_expense"]("info", "food", "Lots of takeout")["status"] == "flagged"
    assert tools["flag_expense"]("info", "beverages", "Lots of coffee")["status"] == "flagged"
    assert "error" in tools["flag_expense"]("info", "groceries", "over the cap")
    assert len(raised) == expense_agent.MAX_AGENT_FLAGS
    assert storage.list_flags("u1")[0]["source"] == "agent"


def test_history_only_counts_weeks_since_first_receipt():
    for d in ("2026-09-29", "2026-10-06", "2026-10-13"):
        storage.save_receipt("u1", "Bar", d, 20, [("Beer", 20.0, "alcohol")])
    tools, _ = make_tools(date(2026, 10, 13))

    history = tools["get_category_history"]("alcohol", 8)
    assert history["weeks_of_history"] == 2
    assert len(history["previous_weeks"]) == 2
    assert history["average_previous_week"] == 20.0  # not diluted by untracked weeks
    assert history["current_week"] == 20.0
    # 2 weeks of history is below the minimum, so the agent can't flag yet.
    assert "error" in tools["flag_expense"]("warning", "alcohol", "Spike")


def test_history_for_first_receipt():
    storage.save_receipt("u1", "Bar", "2026-10-13", 20, [("Beer", 20.0, "alcohol")])
    tools, _ = make_tools(date(2026, 10, 13))
    history = tools["get_category_history"]("alcohol", 8)
    assert history["weeks_of_history"] == 0
    assert history["previous_weeks"] == []
    assert history["average_previous_week"] is None
