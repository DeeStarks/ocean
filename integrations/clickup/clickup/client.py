from loguru import logger
from typing import Any, AsyncGenerator

from port_ocean.utils import http_async_client
from port_ocean.utils.cache import cache_iterator_result


WEBHOOK_NAME = "Port-Ocean-Events-Webhook"
WEBHOOK_EVENTS = [
    "listCreated",
    "listUpdated",
    "listDeleted",
    "taskCreated",
    "taskUpdated",
    "taskDeleted",
]


class ClickupClient:
    def __init__(self, clickup_personal_token: str) -> None:
        self.clickup_url = "https://api.clickup.com/api/v2"
        self.api_auth_header = {"Authorization": clickup_personal_token}
        self.client = http_async_client
        self.client.headers.update(self.api_auth_header)

    async def create_webhook_events(self, app_host: str) -> None:
        for team in await self.get_teams():
            await self._create_team_webhook_events(app_host, team["id"])

    async def _create_team_webhook_events(self, app_host: str, team_id: int) -> None:
        webhook_target_app_host = f"{app_host}/integration/webhook"
        webhooks_response = await self.client.get(
            f"{self.clickup_url}/team/{team_id}/webhook"
        )
        webhooks_response.raise_for_status()
        webhooks = webhooks_response.json()

        existing_webhook = next(
            (
                webhook
                for webhook in webhooks["webhooks"]
                if webhook["endpoint"] == webhook_target_app_host
            ),
            None,
        )

        if existing_webhook:
            logger.info(
                f"Ocean real time reporting clickup webhook already exists [ID: {existing_webhook['id']}]"
            )
            return

        webhook_create_response = await self.client.post(
            f"{self.clickup_url}/team/{team_id}/webhook",
            json={
                "endpoint": webhook_target_app_host,
                "events": WEBHOOK_EVENTS,
            },
        )
        webhook_create_response.raise_for_status()
        webhook_create = webhook_create_response.json()
        logger.info(
            f"Ocean real time reporting clickup webhook created "
            f"[ID: {webhook_create['id']}, Team ID: {team_id}]"
        )

    async def get_teams(self, params: dict[str, Any] = {}) -> list[dict[str, Any]]:
        teams_response = await self.client.get(
            f"{self.clickup_url}/team", params=params
        )
        teams_response.raise_for_status()
        teams = teams_response.json()["teams"]
        return teams

    async def _get_spaces(
        self, team_id: str, params: dict[str, Any] = {}
    ) -> list[dict[str, Any]]:
        spaces_response = await self.client.get(
            f"{self.clickup_url}/team/{team_id}/space", params=params
        )
        spaces_response.raise_for_status()
        return spaces_response.json()["spaces"]

    async def get_projects(self, params: dict[str, Any] = {}) -> list[dict[str, Any]]:
        # getting all teams so as to retrieve the spaces within each team, then using their
        # ids to get the projects (lists) within each space
        projects: list[dict[str, Any]] = []
        for team in await self.get_teams(params):
            spaces = await self._get_spaces(team["id"], params)

            for space in spaces:
                projects_response = await self.client.get(
                    f"{self.clickup_url}/space/{space['id']}/list", params=params
                )
                projects_response.raise_for_status()
                # because the port-app-config uses the team ID to relate the projects to the teams
                # we add the team object to each project
                projects.extend(
                    map(
                        lambda project: {
                            **project,
                            "__team": team,
                        },
                        projects_response.json()["lists"],
                    )
                )
        return projects

    async def get_single_project(self, project_id: str) -> dict[str, Any]:
        # we need to find the most efficient way to get the team object for the project
        # since the project object does not contain the team object
        project_response = await self.client.get(
            f"{self.clickup_url}/list/{project_id}"
        )
        project_response.raise_for_status()
        project = project_response.json()
        project["__team"] = {}

        # we need to find the team object for the project
        team = await self._find_team_for_project(project_id).__anext__()
        if team:
            project["__team"] = team[0]
        return project

    @cache_iterator_result()
    async def _find_team_for_project(
        self, project_id: str
    ) -> AsyncGenerator[list[dict[str, Any]], None]:
        # to cache the result of this function, we need to return an AsyncGenerator
        # hence the need to yield a list of the team object
        # when the team object is found, a list containing a single team object is yielded
        for team in await self.get_teams():
            spaces = await self._get_spaces(team["id"])

            for space in spaces:
                projects_response = await self.client.get(
                    f"{self.clickup_url}/space/{space['id']}/list"
                )
                projects_response.raise_for_status()
                projects = projects_response.json()["lists"]

                if any(project["id"] == project_id for project in projects):
                    yield [team]
                    return

    async def get_paginated_issues(
        self, params: dict[str, Any] = {"page": 0}
    ) -> AsyncGenerator[list[dict[str, Any]], None]:
        for project in await self.get_projects(params):
            while True:
                issues_response = await self.client.get(
                    f"{self.clickup_url}/list/{project['id']}/task", params=params
                )
                issues_response.raise_for_status()
                issues = issues_response.json()
                yield issues["tasks"]

                if issues.get("last_page", False):
                    break

                params["page"] += 1

    async def get_single_issue(self, issue_id: str) -> dict[str, Any]:
        issue_response = await self.client.get(f"{self.clickup_url}/task/{issue_id}")
        issue_response.raise_for_status()
        issue = issue_response.json()
        return issue