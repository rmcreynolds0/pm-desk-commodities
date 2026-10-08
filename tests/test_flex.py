"""
Tests for the IBKR Flex Web Service client.

Flex is the INDEPENDENT record used to check our own ledger, so a bug here is
particularly expensive: a reconciliation that silently always passes is worse
than no reconciliation, because it manufactures confidence. These tests pin
down the two things that could fail quietly — error handling that mistakes a
failure for success, and a diff that misses a real mismatch.

No network. Flex responses are fixtures.
"""
from __future__ import annotations

import pytest

from storage_stress.monitoring import flex


SEND_OK = """<FlexStatementResponse timestamp='01 January, 2026'>
  <Status>Success</Status>
  <ReferenceCode>1234567890</ReferenceCode>
  <Url>https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement</Url>
</FlexStatementResponse>"""

SEND_BAD_TOKEN = """<FlexStatementResponse timestamp='01 January, 2026'>
  <Status>Fail</Status>
  <ErrorCode>1020</ErrorCode>
  <ErrorMessage>Invalid request or unable to validate request.</ErrorMessage>
</FlexStatementResponse>"""

IN_PROGRESS = """<FlexStatementResponse timestamp='01 January, 2026'>
  <Status>Fail</Status>
  <ErrorCode>1019</ErrorCode>
  <ErrorMessage>Statement generation in progress. Please try again shortly.</ErrorMessage>
</FlexStatementResponse>"""

STATEMENT = """<FlexQueryResponse queryName="xsec" type="AF">
 <FlexStatements count="1">
  <FlexStatement accountId="DU1234567" fromDate="20260901" toDate="20260930">
   <EquitySummaryInBase>
     <EquitySummaryByReportDateInBase reportDate="20260929" total="998000.25"/>
     <EquitySummaryByReportDateInBase reportDate="20260930" total="1002345.67"/>
   </EquitySummaryInBase>
   <OpenPositions>
     <OpenPosition symbol="CLZ6" underlyingSymbol="CL" position="3"
                   markPrice="83.17" />
     <OpenPosition symbol="GCZ6" underlyingSymbol="GC" position="-1"
                   markPrice="4425.5" />
     <OpenPosition symbol="ZCZ6" underlyingSymbol="ZC" position="0"
                   markPrice="428.5" />
   </OpenPositions>
   <Trades>
     <Trade symbol="CLZ6" underlyingSymbol="CL" quantity="3"
            tradePrice="83.10" ibOrderID="99" />
   </Trades>
  </FlexStatement>
 </FlexStatements>
</FlexQueryResponse>"""


def test_failure_inside_a_200_is_still_a_failure():
    """Flex signals errors in the BODY with HTTP 200. Reading only the status
    code would treat a bad token as a successful empty statement — which would
    reconcile 'cleanly' against an empty ledger and report all-good."""
    with pytest.raises(flex.FlexError) as e:
        flex._parse_or_raise(SEND_BAD_TOKEN)
    assert e.value.code == "1020"


def test_success_parses():
    root = flex._parse_or_raise(SEND_OK)
    assert root.findtext("ReferenceCode").strip() == "1234567890"


def test_non_xml_response_raises_rather_than_crashing():
    """IBKR occasionally returns an HTML error page."""
    with pytest.raises(flex.FlexError) as e:
        flex._parse_or_raise("<html>maintenance</html>")
    assert e.value.code in {"parse", "?"} or "html" in e.value.message.lower()


def test_in_progress_is_classified_retryable():
    """1019 is the NORMAL first response, not a fault. If it were treated as
    fatal every pull would fail on the first poll."""
    assert "1019" in flex.RETRYABLE
    with pytest.raises(flex.FlexError) as e:
        flex._parse_or_raise(IN_PROGRESS)
    assert e.value.code in flex.RETRYABLE


def test_parses_account_nav_positions_trades():
    snap = flex.parse_statement(STATEMENT)
    assert snap.account_id == "DU1234567"
    assert snap.nav == pytest.approx(1002345.67)
    assert len(snap.positions) == 3
    assert len(snap.trades) == 1


def test_nav_takes_the_most_recent_date():
    """Two dated equity rows; using the first would report stale NAV."""
    snap = flex.parse_statement(STATEMENT)
    assert snap.nav != pytest.approx(998000.25)


def test_net_by_symbol_drops_closed_positions():
    """A zero-quantity row is a closed position, not a holding. Keeping it
    would show as a mismatch against a ledger that correctly deleted it."""
    snap = flex.parse_statement(STATEMENT)
    net = snap.net_by_symbol()
    assert net == {"CL": 3.0, "GC": -1.0}
    assert "ZC" not in net


def test_shorts_stay_negative():
    snap = flex.parse_statement(STATEMENT)
    assert snap.net_by_symbol()["GC"] == -1.0


def test_agreement():
    snap = flex.parse_statement(STATEMENT)
    rec = flex.reconcile(snap, {"CL": 3, "GC": -1})
    assert rec["agree"] is True
    assert rec["mismatches"] == []


def test_ledger_claiming_a_position_the_broker_does_not_hold():
    """The exact bug this project already had: rejected orders written to the
    ledger as fills, so the book claimed holdings IBKR never had."""
    snap = flex.parse_statement(STATEMENT)
    rec = flex.reconcile(snap, {"CL": 3, "GC": -1, "KC": 5})
    assert rec["agree"] is False
    bad = [m for m in rec["mismatches"] if m["symbol"] == "KC"][0]
    assert bad["broker"] == 0 and bad["ledger"] == 5


def test_broker_holding_something_the_ledger_missed():
    snap = flex.parse_statement(STATEMENT)
    rec = flex.reconcile(snap, {"CL": 3})
    assert rec["agree"] is False
    assert any(m["symbol"] == "GC" for m in rec["mismatches"])


def test_wrong_size_is_caught_not_just_wrong_symbol():
    """A partial fill recorded as a full one shows up here and nowhere else."""
    snap = flex.parse_statement(STATEMENT)
    rec = flex.reconcile(snap, {"CL": 5, "GC": -1})
    assert rec["agree"] is False
    m = rec["mismatches"][0]
    assert m["symbol"] == "CL" and m["diff"] == pytest.approx(-2.0)


def test_zero_entries_in_the_ledger_are_ignored():
    """A flat ticker left in the dict must not read as a disagreement."""
    snap = flex.parse_statement(STATEMENT)
    assert flex.reconcile(snap, {"CL": 3, "GC": -1, "ZC": 0})["agree"] is True


def test_both_empty_agrees():
    snap = flex.FlexSnapshot()
    assert flex.reconcile(snap, {})["agree"] is True


def test_missing_credentials_name_what_is_missing(monkeypatch):
    monkeypatch.delenv("FLEX_TOKEN", raising=False)
    monkeypatch.delenv("FLEX_QUERY_ID", raising=False)
    with pytest.raises(flex.FlexError) as e:
        flex.pull()
    assert "FLEX_TOKEN" in str(e.value)
