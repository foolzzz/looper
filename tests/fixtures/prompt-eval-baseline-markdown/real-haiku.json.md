Prompt eval: real/haiku @ c9ac42d5c (3 reps) (no earlier run to compare with)

| task | success | steps | parallel Workers | retries | reviews | gates | room messages | room deliveries | room inbox calls | empty room inbox calls | room inbox calls woken by delivery | contract violations | Planner file edits | tokens (prompt / output) | cost (USD) | first-pass rate | attempts per passing case | cost per passing case (USD) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| single_step | 3/3 | 1.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 105k / 1.7k | 0.0493 | – | – | – |
| dependent_steps | 3/3 | 2.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 228k / 3.6k | 0.0881 | – | – | – |
| many_steps | 3/3 | 9.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | – | – | – | – | 0.3 | 0.0 | 847k / 10.9k | 0.2959 | – | – | – |
| parallel_parts | 3/3 | 3.0 | 2.0 | 0.0 | 0.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 326k / 6.1k | 0.1290 | – | – | – |
| interface_first | 0/3 | 3.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 571k / 9.2k | 0.1793 | – | – | – |
| reviewed_step | 3/3 | 1.0 | 1.0 | 0.0 | 1.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 185k / 3.1k | 0.0786 | – | – | – |
| retry_after_failure | 3/3 | 1.0 | 1.0 | 1.0 | 0.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 261k / 4.6k | 0.0991 | – | – | – |
| chat_correction | 3/3 | 1.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 151k / 2.4k | 0.0602 | – | – | – |
| supplement_replan | 3/3 | 2.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 230k / 4.1k | 0.0920 | – | – | – |
| requirement_edit | 3/3 | 2.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | – | – | – | – | 0.3 | 0.0 | 246k / 4.4k | 0.1127 | – | – | – |
| supplement_no_change | 3/3 | 2.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 250k / 3.6k | 0.0905 | – | – | – |
| profile_choice | 1/3 | 1.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | – | – | – | – | 0.3 | 0.0 | 191k / 2.7k | 0.0665 | – | – | – |
| roles_tester_docs | 3/3 | 3.0 | 2.0 | 0.0 | 0.0 | 0.0 | 0.0 | – | – | – | – | 0.7 | 0.0 | 552k / 10.0k | 0.1852 | – | – | – |
| roles_security_review | 3/3 | 1.0 | 1.0 | 0.0 | 2.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 516k / 9.0k | 0.1713 | – | – | – |
| roles_advisory_review | 2/3 | 1.0 | 1.0 | 0.0 | 2.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 521k / 7.9k | 0.1653 | – | – | – |
| roles_two_reviews | 3/3 | 1.0 | 1.0 | 0.0 | 3.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 751k / 15.1k | 0.2502 | – | – | – |
| roles_multi_platform | 0/3 | 5.3 | 4.0 | 0.0 | 0.0 | 0.0 | 0.0 | – | – | – | – | 0.7 | 0.0 | 1306k / 26.7k | 0.4234 | – | – | – |
| roles_template_fits | 3/3 | 1.0 | 1.0 | 0.0 | 2.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 444k / 8.9k | 0.1607 | – | – | – |
| roles_template_unfit | 3/3 | 1.3 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | – | – | – | – | 0.3 | 0.0 | 169k / 3.0k | 0.0712 | – | – | – |
| roles_not_needed | 3/3 | 1.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 122k / 1.9k | 0.0541 | – | – | – |
| gate_ambiguous | 2/3 | 1.3 | 0.7 | 0.0 | 0.0 | 0.7 | 0.0 | – | – | – | – | 1.3 | 0.0 | 158k / 3.1k | 0.0624 | – | – | – |
| gate_not_needed | 3/3 | 1.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 128k / 2.0k | 0.0540 | – | – | – |
| gate_end_early | 3/3 | 2.0 | 1.0 | 0.0 | 0.0 | 1.0 | 0.0 | – | – | – | – | 1.3 | 0.0 | 204k / 4.5k | 0.0813 | – | – | – |
| gate_parallel_branch | 3/3 | 3.0 | 2.0 | 0.0 | 0.0 | 1.0 | 0.0 | – | – | – | – | 0.7 | 0.0 | 274k / 6.3k | 0.1130 | – | – | – |
| gate_discoverable | 3/3 | 1.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 172k / 2.5k | 0.0624 | – | – | – |
| operator_notes | 3/3 | 1.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 152k / 2.2k | 0.0590 | – | – | – |
| operator_notes_stale | 3/3 | 1.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 236k / 2.9k | 0.0727 | – | – | – |
| room_interface | 3/3 | 2.3 | 2.0 | 0.3 | 0.0 | 0.0 | 1.0 | – | – | – | – | 0.3 | 0.0 | 1056k / 14.9k | 0.2731 | – | – | – |
| room_consult | 0/3 | 2.0 | 1.0 | 0.0 | 0.0 | 0.0 | 1.0 | – | – | – | – | 0.0 | 0.0 | 345k / 7.2k | 0.1271 | – | – | – |
| room_quiet | 3/3 | 1.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 127k / 1.8k | 0.0526 | – | – | – |
| planner_plan_file | 3/3 | 3.0 | 2.0 | 0.0 | 0.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 268k / 6.0k | 0.1210 | – | – | – |
| reviewer_retry_note | 3/3 | 1.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 40k / 1.3k | 0.0307 | – | – | – |
| reviewer_unrelated_note | 3/3 | 1.0 | 1.0 | 2.0 | 2.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 137k / 3.2k | 0.0679 | – | – | – |
| reviewer_elsewhere | 3/3 | 1.0 | 1.0 | 0.0 | 1.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 84k / 1.7k | 0.0364 | – | – | – |
| reviewer_elsewhere_wrong | 3/3 | 1.0 | 1.0 | 1.0 | 2.0 | 0.0 | 0.0 | – | – | – | – | 0.0 | 0.0 | 111k / 3.0k | 0.0622 | – | – | – |
| **total** | 92/105 (88%, 80–93%) | 65.3 | 4.0 | 5.3 | 16.0 | 2.7 | 2.0 | – | – | – | – | 6.3 | 0.0 | 11463k / 201.4k | 4.0985 | – | – | – |

Problems in the new run:
- interface_first (rep 1): bracketed_pairs 0 < 1; parallel_workers 1 < 2
- roles_multi_platform (rep 1): handle(/tip?day=3) answered 200 with ['tip']
- room_consult (rep 1): room_consults 0 < 1
- interface_first (rep 2): bracketed_pairs 0 < 1; parallel_workers 1 < 2
- profile_choice (rep 2): planner_profile_runs 0 < 1
- roles_advisory_review (rep 2): advisory_role_fails 0 < 1
- roles_multi_platform (rep 2): roles_defined 0 < 4; discipline_roles 0 < 4; interface_dependents 0 < 4; contract_violations 1 > 0
- room_consult (rep 2): room_consults 0 < 1
- interface_first (rep 3): bracketed_pairs 0 < 1; parallel_workers 1 < 2
- profile_choice (rep 3): planner_profile_runs 0 < 1
- roles_multi_platform (rep 3): roles_defined 0 < 4; discipline_roles 0 < 4; interface_dependents 0 < 4; contract_violations 1 > 0
- gate_ambiguous (rep 3): task failed: ## Why it ended Planning failed: invalid reply after a repair turn: steps[1] is missing review ## Done - nothing ## Not done - nothing; settings.toml is missing (the user asked for TOML in settings.toml); gates_opened 0 < 1; gates_passed 0 < 1; conclusion_reads 0 < 1
- room_consult (rep 3): room_consults 0 < 1
