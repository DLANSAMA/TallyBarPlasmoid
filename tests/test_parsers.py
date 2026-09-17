from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "io.github.dlansama.tallybar" / "contents" / "code"))

import parsers

def test_parse_limits():
    data = {
        "clientModelConfigs": [
            {
                "label": "Gemini 1.5 Pro",
                "modelId": "gemini-1.5-pro",
                "quotaInfo": {
                    "remainingFraction": 0.5,
                    "resetTime": "2024-01-01T00:00:00Z"
                }
            },
            {
                "label": "Claude 3 Sonnet",
                "modelId": "claude-sonnet-3",
                "quotaInfo": {
                    "remainingFraction": 0.1,
                    "resetTime": "2024-01-01T00:00:00Z"
                }
            },
            {
                "label": "GPT-OSS 120B (Medium)",
                "modelId": "gpt-oss",
                "quotaInfo": {
                    "remainingFraction": 0.4,
                    "resetTime": "2024-01-01T00:00:00Z"
                }
            }
        ]
    }
    limits = parsers.parse_antigravity_limits(data)
    assert len(limits) == 2
    assert limits[0]["label"] == "Gemini"
    assert limits[0]["percent"] == 50.0
    
    assert limits[1]["label"] == "Others"
    assert limits[1]["percent"] == 90.0  # Claude (90% used) is tighter than GPT-OSS (60% used)
    assert "Claude" in limits[1]["sublabel"]
    assert "GPT" in limits[1]["sublabel"]

def test_parse_limits_gpt_4o():
    data = {
        "clientModelConfigs": [
            {
                "label": "GPT-4o",
                "modelId": "gpt-4o",
                "quotaInfo": {
                    "remainingFraction": 0.3,
                    "resetTime": "2024-01-01T00:00:00Z"
                }
            }
        ]
    }
    limits = parsers.parse_antigravity_limits(data)
    assert len(limits) == 1
    assert limits[0]["label"] == "Others"
    assert limits[0]["percent"] == 70.0
    assert "GPT" in limits[0]["sublabel"]


def test_parse_antigravity_limits_choose_priority_is_order_independent():
    # Two Gemini Pro entries at different effort tiers (Low vs High). The
    # predicate order inside choose() (low-effort condition tried before the
    # catch-all) must decide the winner -- NOT the order the upstream API
    # happens to list the entries in. Regression for a bug where an
    # entries-outer any(predicates) collapsed to the loosest predicate and let
    # list order silently decide which effort tier's usage was reported.
    low = {
        "label": "Gemini 3.1 Pro (Low)",
        "modelId": "gemini-3.1-pro-low",
        "quotaInfo": {"remainingFraction": 0.9, "resetTime": "2030-01-01T00:00:00Z"},
    }
    high = {
        "label": "Gemini 3.1 Pro (High)",
        "modelId": "gemini-3.1-pro-high",
        "quotaInfo": {"remainingFraction": 0.1, "resetTime": "2030-01-01T00:00:00Z"},
    }
    data_low_first = {"clientModelConfigs": [low, high]}
    data_high_first = {"clientModelConfigs": [high, low]}
    limits_low_first = parsers.parse_antigravity_limits(data_low_first)
    limits_high_first = parsers.parse_antigravity_limits(data_high_first)
    assert len(limits_low_first) == 1 and limits_low_first[0]["label"] == "Gemini"
    assert len(limits_high_first) == 1 and limits_high_first[0]["label"] == "Gemini"
    # Predicate priority (low-effort condition first) must pick the Low entry
    # (90% remaining -> 10% used) regardless of list order.
    assert round(limits_low_first[0]["percent"], 6) == 10.0
    assert round(limits_high_first[0]["percent"], 6) == 10.0


def test_relative_reset():
    assert parsers.relative_reset("") == ""
    # Test invalid string returns input unchanged
    assert parsers.relative_reset("invalid") == "invalid"
    
    # Future timestamp (days)
    import datetime as dt
    future_time = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2, hours=3)).isoformat()
    reset_str = parsers.relative_reset(future_time)
    assert "Resets in 2d" in reset_str

    # Past timestamp
    past_time = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).isoformat()
    assert parsers.relative_reset(past_time) == "Reset due"


def test_parse_batchexecute_payload():
    payload = ")]}'\n[[\"wrb.fr\",\"rpc1\",\"[\\\"item\\\"]\"]]"
    result = parsers.parse_batchexecute_payload(payload, "rpc1")
    assert result == ["item"]
    
    # Invalid rpc ID
    assert parsers.parse_batchexecute_payload(payload, "rpc2") is None


def test_parse_claude_credit_balance():
    # Valid credit balance
    data = {"amount": 1050, "currency": "USD"}
    res = parsers.parse_claude_credit_balance(data)
    assert res == {
        "label": "Usage credits",
        "amount": 10.50,
        "currency": "USD",
        "source": "claude-prepaid-credits",
    }
    
    # Invalid format
    assert parsers.parse_claude_credit_balance([]) is None


def test_parse_claude_tier():
    # Rate limit tier
    data = {"organizations": [{"rate_limit_tier": "pro_tier"}]}
    assert parsers.parse_claude_tier(data) == "Pro"

    # Plan
    data2 = {"organizations": [{"plan": "free"}]}
    assert parsers.parse_claude_tier(data2) == "Free"


def test_normalize_tier_most_specific_first_ambiguous_input():
    # _TIER_KEYWORDS is a most-specific-first ladder (enterprise, team, ultra,
    # max, plus, pro, free). A string containing BOTH "max" and "pro" keywords
    # must resolve to "Max" -- pinning the ladder's fixed check order against
    # accidentally degrading to e.g. first-keyword-in-the-string behavior,
    # regardless of which keyword appears first positionally in the input.
    assert parsers.normalize_tier("max_pro") == "Max"
    assert parsers.normalize_tier("pro_max") == "Max"
    # Same check for the enterprise/pro pair called out in the source comment.
    assert parsers.normalize_tier("enterprise-pro") == "Enterprise"
    assert parsers.normalize_tier("max_plus") == "Max"


def test_parse_claude_usage():
    # Session quota by remaining fraction
    data = {"five_hour": {"remaining_fraction": 0.8}, "sessionResetsAt": "2026-05-28T04:00:00Z"}
    res = parsers.parse_claude_usage(data)
    assert len(res) == 1
    assert res[0]["label"] == "Session"
    assert abs(res[0]["percent"] - 20.0) < 0.001

    # Session quota by consumed/limit
    data2 = {"five_hour": {"consumed": 4, "limit": 10}}
    res2 = parsers.parse_claude_usage(data2)
    assert len(res2) == 1
    assert res2[0]["label"] == "Session"
    assert res2[0]["percent"] == 40.0


def test_parse_agy_usage():
    text = (
        "Claude Sonnet 4.6 (Thinking)\n"
        "███████████ ░░░░░░░░░░░ 20%\n"
        "20% remaining · Refreshes in 3h 0m\n"
    )
    res = parsers.parse_agy_usage(text)
    assert "Claude Sonnet 4.6 (Thinking)" in res
    assert res["Claude Sonnet 4.6 (Thinking)"]["remaining"] == 20.0
    assert res["Claude Sonnet 4.6 (Thinking)"]["reset"] == "Resets in 3h 0m"


def test_parse_google_one_credits():
    # Setup data where inner[3][0] is [credits, [sec, nanos]]
    import time
    expiry_sec = int(time.time()) + 3600
    data = [[[None, [], []], [None, []], None, [[150.0, [expiry_sec, 0]]], [expiry_sec, 0]]]
    res = parsers.parse_google_one_credits(data)
    assert res is not None
    assert res["credits"] == 150
    assert res["expiration"] != ""


def test_parse_gemini_usage_info():
    import time
    expiry_sec = int(time.time()) + 3600
    data = [None, [[None, 0.45, 1, [expiry_sec, 0]]]]
    res = parsers.parse_gemini_usage_info(data)
    assert len(res) == 1
    assert res[0]["label"] == "Session"
    assert res[0]["percent"] == 45.0


def test_group_agy_model_quota():
    models = {
        "Gemini 1.5 Pro": {"remaining": 60.0, "reset": "Resets in 2h"},
        "Claude 3.5 Sonnet": {"remaining": 80.0, "reset": "Resets in 1h"},
    }
    res = parsers.group_agy_model_quota(models)
    assert "Gemini" in res
    assert "Others" in res
    assert res["Gemini"]["percent"] == 40.0
    assert res["Gemini"]["reset"] == "Resets in 2h"
    assert res["Others"]["percent"] == 20.0
    assert res["Others"]["reset"] == "Resets in 1h"


# ---------------------------------------------------------------------------
# Additional coverage.
# Functions not exercised above: extract_limits_from_json,
# antigravity_model_entries, plus extra branches of parse_claude_usage,
# parse_gemini_usage_info, parse_google_one_credits, and relative_reset.
# ---------------------------------------------------------------------------


def test_extract_limits_from_json_used_limit_and_remaining_total():
    # The OpenAI/Codex-style usage extractor walks nested dicts and turns
    # used/limit and remaining/total pairs into percent rows.
    data = {
        "usage": {
            "primary": {"used": 30, "limit": 120, "resetsAt": "soon"},
            "secondary": {"remaining": 50, "total": 200},
        }
    }
    limits = parsers.extract_limits_from_json(data)
    by_label = {row["label"]: row for row in limits}
    assert by_label["Primary"]["percent"] == 25.0          # 30/120
    assert by_label["Primary"]["reset"] == "soon"
    assert by_label["Primary"]["unit"] == "%"
    assert by_label["Secondary"]["percent"] == 75.0        # 1 - 50/200


def test_extract_limits_from_json_percent_and_remaining_fraction():
    # Direct percent key.
    pct = parsers.extract_limits_from_json({"usedPercent": 80.0})
    assert pct == [{"label": "Usage", "percent": 80.0, "reset": "", "unit": "%"}]

    # remainingFraction -> (1 - fraction) * 100.
    frac = parsers.extract_limits_from_json({"quota": {"remainingFraction": 0.25}})
    assert frac == [{"label": "Usage", "percent": 75.0, "reset": "", "unit": "%"}]


def test_extract_limits_from_json_preferred_labels_ordering():
    # preferred_labels are walked first so they lead the result list.
    data = {"foo": {"used": 5, "limit": 10}, "bar": {"used": 9, "limit": 10}}
    limits = parsers.extract_limits_from_json(data, ("bar",))
    assert [row["label"] for row in limits] == ["Bar", "Foo"]
    assert limits[0]["percent"] == 90.0


def test_extract_limits_from_json_empty_and_garbage():
    assert parsers.extract_limits_from_json({}) == []
    assert parsers.extract_limits_from_json(None) == []
    assert parsers.extract_limits_from_json("not json") == []
    # A dict with no recognizable usage keys yields nothing.
    assert parsers.extract_limits_from_json({"misc": {"foo": "bar"}}) == []


def test_parse_gemini_usage_info_weekly_and_session_sorted():
    import time
    expiry_sec = int(time.time()) + 7200
    data = [
        None,
        [
            [None, 0.6, 2, [expiry_sec, 0]],   # window 2 -> Weekly
            [None, 0.1, 1, [expiry_sec, 0]],   # window 1 -> Session
        ],
    ]
    res = parsers.parse_gemini_usage_info(data)
    # Sorted by window code: Session (1) before Weekly (2).
    assert [r["label"] for r in res] == ["Session", "Weekly"]
    assert res[0]["percent"] == 10.0
    assert res[1]["percent"] == 60.0
    assert res[1]["unit"] == "%"


def test_parse_gemini_usage_info_empty_and_garbage():
    assert parsers.parse_gemini_usage_info([]) == []
    assert parsers.parse_gemini_usage_info("nope") == []
    assert parsers.parse_gemini_usage_info([None, []]) == []
    # Unknown window code (3) is dropped.
    assert parsers.parse_gemini_usage_info([None, [[None, 0.5, 3, [0, 0]]]]) == []


def test_parse_claude_usage_weekly_and_model_windows():
    data = {
        "seven_day": {"utilization": 42.0, "resets_at": "2030-01-01T00:00:00Z"},
        "seven_day_sonnet": {"utilization": 12.0},
    }
    res = parsers.parse_claude_usage(data)
    by_label = {row["label"]: row for row in res}
    assert by_label["Weekly"]["percent"] == 42.0
    assert by_label["Weekly"]["unit"] == "%"
    assert by_label["Sonnet"]["percent"] == 12.0


def test_parse_claude_usage_seven_day_both_sonnet_and_opus():
    # A payload carrying independent non-empty seven_day_sonnet AND
    # seven_day_opus windows must surface BOTH rows -- a match for one model
    # must not short-circuit the other (regression for a nested-loop fix).
    data = {
        "seven_day_sonnet": {"utilization": 42.0, "resets_at": "2030-01-01T00:00:00Z"},
        "seven_day_opus": {"utilization": 88.0, "resets_at": "2030-01-01T00:00:00Z"},
    }
    res = parsers.parse_claude_usage(data)
    by_label = {row["label"]: row for row in res}
    assert by_label["Sonnet"]["percent"] == 42.0
    assert by_label["Opus"]["percent"] == 88.0


def test_parse_claude_usage_seven_day_camel_case_both_models():
    # camelCase aliases for both models must also both surface.
    data = {
        "sevenDaySonnet": {"consumed": 3, "limit": 10},
        "sevenDayOpus": {"consumed": 9, "limit": 10},
    }
    res = parsers.parse_claude_usage(data)
    by_label = {row["label"]: row for row in res}
    assert by_label["Sonnet"]["percent"] == 30.0
    assert by_label["Opus"]["percent"] == 90.0


def test_parse_claude_usage_unknown_model_window_is_discovered_automatically():
    # Anthropic can ship a per-model 7-day pool for any new model without TallyBar
    # having hardcoded its name -- the parser discovers "seven_day_<model>" keys
    # from the payload itself rather than a fixed Sonnet/Opus tuple.
    data = {
        "seven_day_sonnet": {"utilization": 12.0},
        "seven_day_fable": {"utilization": 77.0, "resets_at": "2030-01-01T00:00:00Z"},
    }
    res = parsers.parse_claude_usage(data)
    by_label = {row["label"]: row for row in res}
    assert by_label["Sonnet"]["percent"] == 12.0
    assert by_label["Fable"]["percent"] == 77.0


def test_parse_claude_usage_limits_array_scoped_model_live_shape():
    # Mirror of the live 2026-07-04 /usage payload: every seven_day_<model> flat
    # key is null, and the Fable weekly cap ships ONLY as a "weekly_scoped" entry
    # in the "limits" ARRAY. The array's session/weekly_all entries duplicate the
    # flat five_hour/seven_day keys and must NOT produce duplicate rows; the
    # scoped Fable entry must surface even though is_active is false (the live
    # session window is also is_active: false — it is not an existence gate).
    data = {
        "five_hour": {"utilization": 24.0, "resets_at": "2030-01-01T00:00:00Z"},
        "seven_day": {"utilization": 48.0, "resets_at": "2030-01-05T00:00:00Z"},
        "seven_day_opus": None,
        "seven_day_sonnet": None,
        "limits": [
            {"kind": "session", "group": "session", "percent": 24,
             "resets_at": "2030-01-01T00:00:00Z", "scope": None, "is_active": False},
            {"kind": "weekly_all", "group": "weekly", "percent": 48,
             "resets_at": "2030-01-05T00:00:00Z", "scope": None, "is_active": True},
            {"kind": "weekly_scoped", "group": "weekly", "percent": 44,
             "resets_at": "2030-01-05T00:00:00Z",
             "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
             "is_active": False},
        ],
        "extra_usage": {
            "is_enabled": False,
            "monthly_limit": 2000,       # minor units: $20.00
            "used_credits": 1904.0,      # minor units: $19.04
            "utilization": 95.2,
            "currency": "USD",
            "decimal_places": 2,
        },
    }
    res = parsers.parse_claude_usage(data)
    labels = [row["label"] for row in res]
    assert labels.count("Session") == 1
    assert labels.count("Weekly") == 1
    by_label = {row["label"]: row for row in res}
    assert by_label["Fable"]["percent"] == 44.0
    assert by_label["Fable"]["unit"] == "%"
    assert by_label["Monthly"]["used"] == 19.04
    assert by_label["Monthly"]["limit"] == 20.0


def test_parse_claude_usage_limits_array_only():
    # If the flat five_hour/seven_day keys are retired outright, the array alone
    # must still yield Session/Weekly/scoped rows (session-kind => 5h window).
    data = {
        "limits": [
            {"kind": "session", "group": "session", "percent": 10,
             "resets_at": "2030-01-01T00:00:00Z", "scope": None, "is_active": False},
            {"kind": "weekly_all", "group": "weekly", "percent": 55,
             "resets_at": "2030-01-05T00:00:00Z", "scope": None, "is_active": True},
            {"kind": "weekly_scoped", "group": "weekly", "percent": 70,
             "resets_at": "2030-01-05T00:00:00Z",
             "scope": {"model": {"display_name": "Fable"}}},
        ],
    }
    res = parsers.parse_claude_usage(data)
    by_label = {row["label"]: row for row in res}
    assert by_label["Session"]["percent"] == 10.0
    assert by_label["Weekly"]["percent"] == 55.0
    assert by_label["Fable"]["percent"] == 70.0


def test_parse_claude_usage_limits_array_deduped_against_flat_model_key():
    # Should a payload ever carry BOTH a seven_day_<model> flat key and the same
    # model as a scoped array entry, the flat key (parsed first) wins.
    data = {
        "seven_day_fable": {"utilization": 77.0},
        "limits": [
            {"kind": "weekly_scoped", "group": "weekly", "percent": 44,
             "scope": {"model": {"display_name": "Fable"}}},
        ],
    }
    res = parsers.parse_claude_usage(data)
    fable_rows = [row for row in res if row["label"] == "Fable"]
    assert len(fable_rows) == 1
    assert fable_rows[0]["percent"] == 77.0


def test_parse_claude_usage_extra_usage_decimal_places_scales_all_money():
    # decimal_places in the extra_usage block is authoritative for every money
    # field in it — including aliases otherwise assumed to be dollars
    # (monthly_limit) — so used and limit can't end up in mixed units.
    data = {
        "extra_usage": {
            "monthly_limit": 2000,
            "used_credits": 1904.0,
            "utilization": 95.2,
            "currency": "USD",
            "decimal_places": 2,
        }
    }
    res = parsers.parse_claude_usage(data)
    monthly = [row for row in res if row["label"] == "Monthly"][0]
    assert monthly["used"] == 19.04
    assert monthly["limit"] == 20.0


def test_parse_claude_usage_extra_usage_overage_cents():
    # used_credits / monthly_credit_limit arrive in CENTS and must be /100.
    data = {
        "extra_usage": {
            "used_credits": 1500,           # -> $15.00
            "monthly_credit_limit": 5000,   # -> $50.00
            "currency": "USD",
            "is_enabled": True,
        }
    }
    res = parsers.parse_claude_usage(data)
    assert len(res) == 1
    monthly = res[0]
    assert monthly["label"] == "Monthly"
    assert monthly["percent"] == 30.0      # 15/50
    assert monthly["used"] == 15.0
    assert monthly["limit"] == 50.0
    assert monthly["currency"] == "USD"
    assert monthly["reset"] == "Enterprise spend limit"


def test_parse_claude_usage_extra_usage_dollars_no_double_divide():
    # Dollar-denominated aliases are NOT divided by 100.
    data = {
        "extra_usage": {
            "utilization": 25.0,
            "monthly_consumed": 12.5,
            "credit_limit": 50.0,
            "currency": "EUR",
        }
    }
    res = parsers.parse_claude_usage(data)
    assert len(res) == 1
    monthly = res[0]
    assert monthly["percent"] == 25.0
    assert monthly["used"] == 12.5
    assert monthly["limit"] == 50.0
    assert monthly["currency"] == "EUR"
    assert monthly["unit"] == "EUR"


def test_parse_claude_usage_not_dict():
    assert parsers.parse_claude_usage([]) == []
    assert parsers.parse_claude_usage(None) == []


def test_parse_claude_usage_extra_usage_dpless_credits_pair_consistent():
    # dp-absent: used_credits (cents alias) paired with monthly_limit (dollar alias).
    # Pair-consistency rule: EITHER side matching a *_credits/* alias scales BOTH /100.
    # Result: $19.04 of $20.00, not $19.04 of $2000.00.
    data = {
        "extra_usage": {
            "used_credits": 1904,
            "monthly_limit": 2000,
            "currency": "USD",
        }
    }
    res = parsers.parse_claude_usage(data)
    assert len(res) == 1
    monthly = res[0]
    assert monthly["used"] == pytest.approx(19.04, rel=1e-6)
    assert monthly["limit"] == pytest.approx(20.00, rel=1e-6)
    assert monthly["percent"] == pytest.approx(95.2, rel=1e-3)


def test_parse_claude_usage_extra_usage_dpless_plain_pair_unscaled():
    # dp-absent: dollar-denominated aliases on both sides — neither scaled.
    data = {
        "extra_usage": {
            "monthly_consumed": 15.0,
            "monthly_limit": 50.0,
            "currency": "USD",
        }
    }
    res = parsers.parse_claude_usage(data)
    assert len(res) == 1
    monthly = res[0]
    assert monthly["used"] == pytest.approx(15.0, rel=1e-6)
    assert monthly["limit"] == pytest.approx(50.0, rel=1e-6)
    assert monthly["percent"] == pytest.approx(30.0, rel=1e-3)


def test_antigravity_model_entries_all_sources():
    data = {
        "clientModelConfigs": [
            {
                "label": "Gemini 3 Pro",
                "modelId": "gemini-3-pro",
                "quotaInfo": {"remainingFraction": 0.6, "resetTime": "2030-01-01T00:00:00Z"},
            },
            {"label": "no-quota", "modelId": "x"},  # dropped: no remainingFraction
        ],
        "models": {
            "claude-sonnet": {
                "displayName": "Claude Sonnet 4.6",
                "quotaInfo": {"remainingFraction": 0.2},
            },
        },
        "buckets": [
            {"modelId": "gpt-oss", "label": "GPT-OSS", "remainingFraction": 0.9, "resetTime": "later"},
        ],
    }
    entries = parsers.antigravity_model_entries(data)
    # (label, model_id, remaining, reset) tuples; the no-quota config is dropped.
    assert ("Gemini 3 Pro", "gemini-3-pro", 0.6, "2030-01-01T00:00:00Z") in entries
    assert ("Claude Sonnet 4.6", "claude-sonnet", 0.2, "") in entries
    assert ("GPT-OSS", "gpt-oss", 0.9, "later") in entries
    assert len(entries) == 3


def test_antigravity_model_entries_user_status_cascade():
    # Configs nested under userStatus.cascadeModelConfigData are also collected.
    data = {
        "userStatus": {
            "cascadeModelConfigData": {
                "clientModelConfigs": [
                    {
                        "displayName": "Gemini Flash",
                        "modelId": "gemini-flash",
                        "quotaInfo": {"remainingFraction": 0.75},
                    }
                ]
            }
        }
    }
    entries = parsers.antigravity_model_entries(data)
    assert entries == [("Gemini Flash", "gemini-flash", 0.75, "")]


def test_antigravity_model_entries_non_dict_and_empty():
    assert parsers.antigravity_model_entries("nope") == []
    assert parsers.antigravity_model_entries({}) == []


def test_parse_google_one_credits_empty_and_garbage():
    assert parsers.parse_google_one_credits([]) is None
    assert parsers.parse_google_one_credits("x") is None
    assert parsers.parse_google_one_credits(None) is None
    # Inner structure present but the credit pool is empty.
    assert parsers.parse_google_one_credits([[[None], [None], None, []]]) is None


def test_parse_google_one_credits_nan_and_infinity_returns_none():
    # A NaN or Infinity credits value must return None instead of raising
    # (int(nan) -> ValueError, int(inf) -> OverflowError).
    expiry_sec = 123
    nan_data = [[[None, [], []], [None, []], None, [[float("nan"), [expiry_sec, 0]]], [expiry_sec, 0]]]
    assert parsers.parse_google_one_credits(nan_data) is None
    inf_data = [[[None, [], []], [None, []], None, [[float("inf"), [expiry_sec, 0]]], [expiry_sec, 0]]]
    assert parsers.parse_google_one_credits(inf_data) is None
    neg_inf_data = [[[None, [], []], [None, []], None, [[float("-inf"), [expiry_sec, 0]]], [expiry_sec, 0]]]
    assert parsers.parse_google_one_credits(neg_inf_data) is None


def test_relative_reset_minutes_hours_and_none():
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)

    # Minutes-only branch (under an hour).
    minutes = parsers.relative_reset((now + dt.timedelta(minutes=42)).isoformat())
    assert minutes.startswith("Resets in ") and minutes.endswith("m")
    assert "h" not in minutes and "d" not in minutes

    # Hours branch (under a day).
    hours = parsers.relative_reset((now + dt.timedelta(hours=3, minutes=20)).isoformat())
    assert hours.startswith("Resets in 3h ") and hours.endswith("m")

    # None / falsy input -> empty string.
    assert parsers.relative_reset(None) == ""



# ---- retrieveUserQuotaSummary -> per-group, per-window lanes (matches agy /usage) --------------

_QUOTA_SUMMARY_SAMPLE = {
    "groups": [
        {
            "displayName": "Gemini Models",
            "buckets": [
                {"bucketId": "gemini-weekly", "window": "weekly",
                 "resetTime": "2030-06-28T03:18:59Z", "remainingFraction": 0.9093388},
                {"bucketId": "gemini-5h", "window": "5h",
                 "resetTime": "2030-06-24T01:03:34Z", "remainingFraction": 0.8130025},
            ],
        },
        {
            "displayName": "Claude and GPT models",
            "buckets": [
                {"bucketId": "3p-weekly", "window": "weekly",
                 "resetTime": "2030-06-28T05:36:44Z", "remainingFraction": 0.2153392},
                {"bucketId": "3p-5h", "window": "5h",
                 "resetTime": "2030-06-24T02:52:55Z", "remainingFraction": 0.0664672},
            ],
        },
    ],
}


def test_parse_antigravity_quota_summary_four_lanes_group_major():
    lanes = parsers.parse_antigravity_quota_summary(_QUOTA_SUMMARY_SAMPLE)
    # 2 groups x 2 windows = 4 lanes, ordered group-major: each group's 5-hour then weekly, kept
    # together ("Geminis together, Claude/GPTs together") — and 5-hour-first even though the API
    # lists weekly first per group.
    assert [(l["label"], l["sublabel"]) for l in lanes] == [
        ("Gemini", "5-hour"),
        ("Gemini", "Weekly"),
        ("Claude · GPT", "5-hour"),
        ("Claude · GPT", "Weekly"),
    ]
    # % used = (1 - remainingFraction) * 100, matched to agy /usage.
    by = {(l["label"], l["sublabel"]): l for l in lanes}
    assert round(by[("Gemini", "5-hour")]["percent"], 2) == 18.70
    assert round(by[("Claude · GPT", "5-hour")]["percent"], 2) == 93.35
    assert round(by[("Gemini", "Weekly")]["percent"], 2) == 9.07
    assert round(by[("Claude · GPT", "Weekly")]["percent"], 2) == 78.47
    for l in lanes:
        assert l["unit"] == "%"
        assert l["reset"].startswith("Resets in ")   # far-future resetTime -> a real countdown


def test_parse_antigravity_quota_summary_infers_window_from_bucket_id():
    # window field absent -> fall back to the bucketId suffix.
    data = {"groups": [{"displayName": "Gemini Models", "buckets": [
        {"bucketId": "gemini-5h", "resetTime": "2030-01-01T00:00:00Z", "remainingFraction": 0.5},
    ]}]}
    lanes = parsers.parse_antigravity_quota_summary(data)
    assert len(lanes) == 1 and lanes[0]["window"] == "5h" and lanes[0]["sublabel"] == "5-hour"
    assert round(lanes[0]["percent"], 1) == 50.0


def test_parse_antigravity_quota_summary_rejects_garbage():
    assert parsers.parse_antigravity_quota_summary(None) == []
    assert parsers.parse_antigravity_quota_summary({}) == []
    assert parsers.parse_antigravity_quota_summary({"groups": "nope"}) == []
    # bucket without remainingFraction (or a bool one) is skipped, not counted as 0%.
    data = {"groups": [{"displayName": "Gemini Models", "buckets": [
        {"window": "5h", "resetTime": "2030-01-01T00:00:00Z"},
        {"window": "weekly", "resetTime": "2030-01-01T00:00:00Z", "remainingFraction": True},
    ]}]}
    assert parsers.parse_antigravity_quota_summary(data) == []


def test_extract_limits_from_json_depth_capped():
    # A pathologically nested response must not recurse unboundedly; a shallow
    # limit-shaped node still parses.
    deep = {"usedPercent": 50}
    for _ in range(40):
        deep = {"wrap": deep}
    assert parsers.extract_limits_from_json(deep) == []
    shallow = {"a": {"b": {"session": {"usedPercent": 50}}}}
    assert parsers.extract_limits_from_json(shallow)[0]["percent"] == 50.0


# ---------------------------------------------------------------------------
# parse_grok_billing_config (main-tree unique)
# ---------------------------------------------------------------------------

def test_parse_grok_billing_config_weekly_credits():
    event = {
        "msg": "billing: fetched credits config",
        "ctx": {
            "config": {
                "creditUsagePercent": 85.0,
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "start": "2026-07-04T13:20:44.991585+00:00",
                    "end": "2030-01-01T00:00:00+00:00",
                },
            },
            "subscriptionTier": "SuperGrok",
        },
    }
    parsed = parsers.parse_grok_billing_config(event)
    assert parsed is not None
    assert parsed["tier"] == "SuperGrok"
    assert parsed["percentExplicit"] is True
    assert len(parsed["limits"]) == 1
    limit = parsed["limits"][0]
    assert limit["label"] == "Weekly"
    assert limit["percent"] == 85.0
    assert limit["unit"] == "percent"
    assert "Resets" in limit["reset"] or limit["reset"] == "Reset due"
    assert limit.get("resetAt", "").startswith("2030-01-01")


def test_parse_grok_billing_config_clamps_and_rejects():
    assert parsers.parse_grok_billing_config(None) is None
    assert parsers.parse_grok_billing_config({}) is None
    assert parsers.parse_grok_billing_config({"ctx": {"config": {}}}) is None
    high = parsers.parse_grok_billing_config({
        "ctx": {"config": {"creditUsagePercent": 150}, "subscriptionTier": "Free"},
    })
    assert high is not None
    assert high["limits"][0]["percent"] == 100.0
    assert high["tier"] == "Free"
    assert high["percentExplicit"] is True


def test_parse_grok_billing_config_missing_percent_defaults_to_zero():
    """Fresh weekly period often omits creditUsagePercent (historyLen==0).

    With a usable currentPeriod we still synthesise a 0% Weekly bar so the
    widget doesn't go api-empty after reset.
    """
    event = {
        "msg": "billing: fetched credits config",
        "ctx": {
            "config": {
                "currentPeriod": {
                    "type": "USAGE_PERIOD_TYPE_WEEKLY",
                    "start": "2026-07-18T13:20:44.991585+00:00",
                    "end": "2026-07-25T13:20:44.991585+00:00",
                },
                "historyLen": 0,
            },
            "subscriptionTier": "SuperGrok",
        },
    }
    parsed = parsers.parse_grok_billing_config(event)
    assert parsed is not None
    assert parsed["percentExplicit"] is False
    assert parsed["tier"] == "SuperGrok"
    assert parsed["limits"][0]["percent"] == 0.0
    assert parsed["limits"][0]["label"] == "Weekly"
    assert parsed["period"]["start"].startswith("2026-07-18")
    assert parsed["limits"][0]["resetAt"].startswith("2026-07-25")


def test_as_dict_narrows_payload_values():
    """as_dict replaces the `d.get(k) if isinstance(d.get(k), dict) else {}` idiom.

    Behaviour must be identical to the idiom it replaces — the point of the change
    is the type (and calling .get once instead of twice), not the semantics.
    """
    assert parsers.as_dict({"a": 1}) == {"a": 1}

    # Non-dicts fall back to {}.
    for junk in (None, [], "x", 3, True, object()):
        assert parsers.as_dict(junk) == {}

    # An explicit dict default is used only when the value isn't a dict.
    fallback = {"from": "default"}
    assert parsers.as_dict(None, fallback) is fallback
    assert parsers.as_dict({"real": 1}, fallback) == {"real": 1}

    # A non-dict default still degrades to {} rather than leaking the junk out.
    assert parsers.as_dict(None, "not-a-dict") == {}
