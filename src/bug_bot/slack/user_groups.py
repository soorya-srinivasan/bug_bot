"""Slack user groups API: list groups and list users in a group."""

from slack_sdk.web.async_client import AsyncWebClient

from bug_bot.config import settings


def _get_client() -> AsyncWebClient:
    return AsyncWebClient(token=settings.slack_bot_token)


async def list_user_groups(
    include_disabled: bool = False,
    include_count: bool = True,
) -> list[dict]:
    """
    List all user groups in the workspace.
    Returns list of dicts with id, name, handle, description, user_count, etc.
    """
    client = _get_client()
    resp = await client.usergroups_list(
        include_disabled=include_disabled,
        include_count=include_count,
    )
    if not resp.get("ok"):
        raise ValueError(resp.get("error", "usergroups.list failed"))
    return resp.get("usergroups", [])


async def list_users_in_group(
    usergroup_id: str,
    include_disabled: bool = False,
    include_user_details: bool = False,
) -> dict:
    """
    List user IDs (and optionally user details) in a Slack user group.

    Returns dict with:
      - usergroup_id: str
      - user_ids: list[str]
      - users: list[dict] (only when include_user_details=True), each with
          id, name, real_name, profile.real_name, profile.display_name, is_bot, etc.
    """
    client = _get_client()
    resp = await client.usergroups_users_list(
        usergroup=usergroup_id,
        include_disabled=include_disabled,
    )
    if not resp.get("ok"):
        raise ValueError(resp.get("error", "usergroups.users.list failed"))

    user_ids: list[str] = resp.get("users", [])

    result: dict = {
        "usergroup_id": usergroup_id,
        "user_ids": user_ids,
    }

    if include_user_details and user_ids:
        users = []
        for uid in user_ids:
            u_resp = await client.users_info(user=uid)
            if u_resp.get("ok") and u_resp.get("user"):
                u = u_resp["user"]
                users.append({
                    "id": u.get("id"),
                    "name": u.get("name"),
                    "real_name": u.get("real_name"),
                    "display_name": u.get("profile", {}).get("display_name") or u.get("name"),
                    "is_bot": u.get("is_bot", False),
                    "deleted": u.get("deleted", False),
                })
            else:
                users.append({"id": uid, "name": None, "real_name": None, "display_name": None, "is_bot": False, "deleted": True})
        result["users"] = users

    return result
