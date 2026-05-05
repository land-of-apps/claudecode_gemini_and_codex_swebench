TODO — AUTHOR a user-POV bug report here. Delete everything
below this TODO line before using this fixture.

Engineer-POV reference (NOT for the agent — paraphrase as
customer/operator prose):
  BUG-0001: regression — same-day cutoff inclusive at 16:45 (off-by-one)

Hidden test that will verify the fix:
  :payments-hub → AchCutoffPolicyTest.rejects_submission_exactly_at_final_cutoff

Authoring rules:
  - Do NOT name the failing method, class, file, or
    directly-affected layer.
  - Describe what a user observed (a customer, an
    operations engineer reading dashboards, a finance
    reconciliation, an integration partner).
  - Include "steps to reproduce" if the bug has a
    deterministic trigger.
  - Mention what was expected vs what happened.
  - Avoid comparative cues that pin the layer ("X works
    but Y doesn't" — these collapse the symptom-to-
    location gap).

Reference: see issue.md files in synth_bugs/oscar_4016,
oscar_n1_basket, etc. for the desired voice.
