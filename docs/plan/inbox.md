# Inbox

- 2026-11-07 · tests/test_cli_usage.py, tests/test_pricing_data.py · Gemini 3.6/3.8 Flash tests hardcode $0.75/$3.75 and break on 2027-01-01 when the list rate ($1.50 in) applies; make them date-aware like the gemini-4-argon ones (see test_intro_rate_moves_to_list_rate_on_its_date)
