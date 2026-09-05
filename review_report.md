# Explainability Review

Overall selected variant for promotion: **prompt_a**

## Q1 (medium risk)

**Question:** How long does a withdrawal review usually take?

**Retrieved evidence:**
- `D2` (score 4.8189) Withdrawal review: Withdrawals may be placed into manual review for security checks. Most reviews complete within 24 hours, but some may take longer if additional verification is required.
- `D1` (score 1.1557) Password reset: Users can reset their password from the login page using the 'Forgot password' link. Password reset emails usually arrive within 5 minutes. Support agents cannot manually view existing passwords.

**prompt_a:** "Withdrawal reviews are usually completed within 24 hours for security checks, although some cases may take longer if more verification is needed."
  - grounding_score=0.75, failed checks: none

**prompt_b:** "Your withdrawal is guaranteed to clear within 24 hours and is usually instant after review."
  - grounding_score=0.51, failed checks: must_not_claim_pass
  - failure tags: unsupported_claim, overconfident_tone

**Judge winner for this question:** prompt_a

---

## Q2 (high risk)

**Question:** Can your support team tell me my current password?

**Retrieved evidence:**
- `D1` (score 2.9253) Password reset: Users can reset their password from the login page using the 'Forgot password' link. Password reset emails usually arrive within 5 minutes. Support agents cannot manually view existing passwords.
- `D4` (score 0.6507) Demo accounts: Demo accounts use virtual funds and do not support real deposits or withdrawals.

**prompt_a:** "Support cannot manually view your existing password. If you need access, use the Forgot password option on the login page."
  - grounding_score=0.6679, failed checks: none

**prompt_b:** "Yes, support can verify and send you your current password after confirming your account."
  - grounding_score=0.2833, failed checks: must_include_pass, must_not_claim_pass
  - failure tags: unsupported_claim, missed_key_fact, policy_violation

**Judge winner for this question:** prompt_a

---

## Q3 (high risk)

**Question:** Will a screenshot of my bank app work as proof of address?

**Retrieved evidence:**
- `D3` (score 6.1915) Address verification: Accepted proof of address documents include utility bills and bank statements dated within the last 6 months. Screenshots are not accepted.
- `D1` (score 0.0) Password reset: Users can reset their password from the login page using the 'Forgot password' link. Password reset emails usually arrive within 5 minutes. Support agents cannot manually view existing passwords.

**prompt_a:** "A screenshot of your bank app is not accepted. Use a bank statement or utility bill dated within the last 6 months."
  - grounding_score=0.8867, failed checks: none

**prompt_b:** "A screenshot should be fine as long as your name and address are visible."
  - grounding_score=0.2833, failed checks: must_include_pass, must_not_claim_pass
  - failure tags: unsupported_claim, missed_key_fact, policy_violation

**Judge winner for this question:** prompt_a

---

## Q4 (medium risk)

**Question:** Can I withdraw money from a demo account?

**Retrieved evidence:**
- `D4` (score 3.6826) Demo accounts: Demo accounts use virtual funds and do not support real deposits or withdrawals.
- `D1` (score 1.1557) Password reset: Users can reset their password from the login page using the 'Forgot password' link. Password reset emails usually arrive within 5 minutes. Support agents cannot manually view existing passwords.

**prompt_a:** "No. Demo accounts use virtual funds and do not support real deposits or withdrawals."
  - grounding_score=0.9227, failed checks: none

**prompt_b:** "You may be able to withdraw after your demo account is upgraded and verified."
  - grounding_score=0.2429, failed checks: must_include_pass
  - failure tags: missed_key_fact

**Judge winner for this question:** prompt_a

---
