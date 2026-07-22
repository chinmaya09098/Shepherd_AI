"""
src/security — Application-level security middleware for Shepherd AI.

Modules
-------
apim_guard      Validate APIM subscription key on HTTP triggers.
input_validator Sanitize email bodies and attachments before AI processing.
prompt_guard    Detect and neutralise prompt injection in LLM inputs.
rbac            Enforce Azure AD role-based access on admin endpoints.
audit           Emit structured security events to Application Insights.
"""
