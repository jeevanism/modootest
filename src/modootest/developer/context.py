"""Context switching helpers for modootest developer API (as_user, as_company)."""
from contextlib import contextmanager
from typing import Any


@contextmanager
def as_user(env: Any, user: Any):
    """Context manager yielding an Odoo Environment derived with the given user.

    Shares the existing cursor, database transaction, and savepoint.
    Derives the environment with su=False (unless target is the superuser),
    ensuring security rules and access rights are enforced as expected.
    Does NOT call Environment.clear() on exit to preserve shared transaction state.

    :param env: Active Odoo Environment instance.
    :param user: res.users recordset, integer user ID, or string XML-ID (e.g. 'base.user_demo').
    :return: Context manager yielding derived Environment attached to user.
    """
    if isinstance(user, str):
        target = env.ref(user)
    elif isinstance(user, int):
        target = user
    elif hasattr(user, "id") and isinstance(user.id, int):
        target = user
    else:
        raise TypeError(
            f"as_user expects a res.users recordset, integer user ID, or XML-ID string; "
            f"got {type(user).__name__}: {user!r}"
        )

    derived_env = env(user=target, su=False)
    try:
        yield derived_env
    finally:
        # Deliberately do NOT call derived_env.clear() on exit, as this would clear
        # shared caches across the entire test transaction. TestTransaction owns cleanup.
        pass


@contextmanager
def as_company(env: Any, company: Any):
    """Context manager yielding an Odoo Environment derived with the given active company.

    Shares the existing cursor, database transaction, and savepoint.
    Follows Odoo 19 allowed_company_ids protocol, ensuring the target company is
    placed at index 0 of context['allowed_company_ids'].
    Does NOT call Environment.clear() on exit.

    :param env: Active Odoo Environment instance.
    :param company: res.company recordset, integer company ID, or string XML-ID (e.g. 'base.main_company').
    :return: Context manager yielding derived Environment attached to company.
    """
    if isinstance(company, str):
        company_record = env.ref(company)
        company_id = int(company_record.id)
    elif isinstance(company, int):
        company_id = company
    elif hasattr(company, "id") and isinstance(company.id, int):
        company_id = int(company.id)
    else:
        raise TypeError(
            f"as_company expects a res.company recordset, integer company ID, or XML-ID string; "
            f"got {type(company).__name__}: {company!r}"
        )

    allowed_company_ids = list(env.context.get("allowed_company_ids") or [])
    if company_id in allowed_company_ids:
        allowed_company_ids.remove(company_id)
    allowed_company_ids.insert(0, company_id)

    new_context = dict(env.context)
    new_context["allowed_company_ids"] = allowed_company_ids
    new_context["company_id"] = company_id

    derived_env = env(context=new_context)
    try:
        yield derived_env
    finally:
        pass
