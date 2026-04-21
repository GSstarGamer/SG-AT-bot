from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import aiohttp


USER_ID_PATTERN = re.compile(r"roblox\.com/users/(?P<user_id>\d+)", re.IGNORECASE)
PROFILE_URL_TEMPLATE = "https://www.roblox.com/users/{user_id}/profile"


class RobloxAPIError(Exception):
    pass


@dataclass(slots=True)
class RobloxUser:
    user_id: int
    username: str
    display_name: str
    profile_url: str
    avatar_url: str | None = None
    place_id: int | None = None
    game_id: str | None = None

    @property
    def label(self) -> str:
        if self.display_name.lower() == self.username.lower():
            return self.username
        return f"{self.display_name} (@{self.username})"

    @property
    def join_url(self) -> str:
        if self.place_id:
            base_url = f"https://www.roblox.com/games/start?placeId={self.place_id}"
            if self.game_id:
                return f"{base_url}&gameInstanceId={quote(self.game_id)}"
            return base_url
        return self.profile_url


class RobloxClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session

    async def resolve_user(self, raw_value: str) -> RobloxUser:
        value = raw_value.strip()
        if not value:
            raise RobloxAPIError("A Roblox username or profile URL is required.")

        user_id = self.extract_user_id(value)
        if user_id is None:
            user = await self.get_user_by_username(value)
        else:
            user = await self.get_user_by_id(user_id)

        await self.populate_users([user])
        return user

    async def populate_users(self, users: list[RobloxUser]) -> None:
        if not users:
            return

        user_ids = [user.user_id for user in users]
        avatar_images = await self.get_avatar_images(user_ids)
        presences = await self.get_presences(user_ids)

        for user in users:
            user.avatar_url = avatar_images.get(user.user_id)
            presence = presences.get(user.user_id)
            if presence is None:
                continue

            place_id = presence.get("placeId")
            game_id = presence.get("gameId")
            if isinstance(place_id, int):
                user.place_id = place_id
            if isinstance(game_id, str) and game_id:
                user.game_id = game_id

    async def get_user_by_username(self, username: str) -> RobloxUser:
        data = await self._request_json(
            "POST",
            "https://users.roblox.com/v1/usernames/users",
            json={
                "usernames": [username],
                "excludeBannedUsers": False,
            },
        )
        matches = data.get("data", [])
        if not matches:
            raise RobloxAPIError(f"Could not find a Roblox user for `{username}`.")
        return self._from_user_payload(matches[0])

    async def get_user_by_id(self, user_id: int) -> RobloxUser:
        data = await self._request_json(
            "GET",
            f"https://users.roblox.com/v1/users/{user_id}",
        )
        return self._from_user_payload(data)

    async def get_avatar_images(self, user_ids: list[int]) -> dict[int, str]:
        joined_ids = ",".join(str(user_id) for user_id in user_ids)
        data = await self._request_json(
            "GET",
            (
                "https://thumbnails.roblox.com/v1/users/avatar"
                f"?userIds={joined_ids}&size=420x420&format=Png&isCircular=false"
            ),
        )
        results: dict[int, str] = {}
        for item in data.get("data", []):
            target_id = item.get("targetId")
            image_url = item.get("imageUrl")
            if isinstance(target_id, int) and isinstance(image_url, str) and image_url:
                results[target_id] = image_url
        return results

    async def get_presences(self, user_ids: list[int]) -> dict[int, dict[str, Any]]:
        data = await self._request_json(
            "POST",
            "https://presence.roblox.com/v1/presence/users",
            json={"userIds": user_ids},
        )
        results: dict[int, dict[str, Any]] = {}
        for item in data.get("userPresences", []):
            user_id = item.get("userId")
            if isinstance(user_id, int):
                results[user_id] = item
        return results

    @staticmethod
    def extract_user_id(value: str) -> int | None:
        match = USER_ID_PATTERN.search(value)
        if match is None:
            return None
        return int(match.group("user_id"))

    @staticmethod
    def _from_user_payload(payload: dict[str, Any]) -> RobloxUser:
        user_id = payload.get("id")
        username = payload.get("name")
        display_name = payload.get("displayName")
        if not isinstance(user_id, int) or not isinstance(username, str):
            raise RobloxAPIError("Roblox returned an invalid user payload.")
        if not isinstance(display_name, str):
            display_name = username
        return RobloxUser(
            user_id=user_id,
            username=username,
            display_name=display_name,
            profile_url=PROFILE_URL_TEMPLATE.format(user_id=user_id),
        )

    async def _request_json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        async with self.session.request(method, url, **kwargs) as response:
            if response.status >= 400:
                message = await response.text()
                raise RobloxAPIError(
                    f"Roblox API request failed with {response.status}: {message}"
                )
            return await response.json()
