"""GitHub operations via the gh CLI."""

import logging

from .shell import Shell


class GitHubClient:
    """Minimal gh wrapper for issues, comments, and Projects v2."""

    def __init__(self, owner, repo, project_number, project_title, shell=None, logger=None):
        self.owner = owner
        self.repo = repo
        self.project_number = project_number
        self.project_title = project_title
        self.shell = shell or Shell(logger=logger)
        self.logger = logger or logging.getLogger(__name__)
        self._project_cache = None
        self._viewer_login = None

    def repo_slug(self):
        """Return owner/repo."""
        return f"{self.owner}/{self.repo}"

    def viewer_login(self):
        """Return the gh-authenticated user login."""
        if self._viewer_login:
            return self._viewer_login
        data = self.shell.run_json(["gh", "api", "user"])
        login = data.get("login")
        if not login:
            raise RuntimeError("Unable to resolve gh viewer login")
        self._viewer_login = login
        return login

    def list_open_issues(self, label, limit=100):
        """List open issues with a label."""
        data = self.shell.run_json(
            [
                "gh",
                "issue",
                "list",
                "-R",
                self.repo_slug(),
                "--label",
                label,
                "--state",
                "open",
                "--limit",
                str(limit),
                "--json",
                "number,title,createdAt",
            ]
        )
        return data or []

    def get_issue(self, number):
        """Fetch issue details as JSON."""
        return self.shell.run_json(
            [
                "gh",
                "issue",
                "view",
                str(number),
                "-R",
                self.repo_slug(),
                "--json",
                "number,title,body,state,labels",
            ]
        )

    def comment_issue(self, number, body):
        """Post a comment on an issue."""
        self.shell.run(
            [
                "gh",
                "issue",
                "comment",
                str(number),
                "-R",
                self.repo_slug(),
                "--body",
                body,
            ]
        )

    def delete_last_comment(self, number):
        """Delete the last comment authored by the current user."""
        self.shell.run(
            [
                "gh",
                "issue",
                "comment",
                str(number),
                "-R",
                self.repo_slug(),
                "--delete-last",
                "--yes",
            ],
            check=False,
        )

    def get_latest_claim(self, number):
        """Return the most recent claim comment if present."""
        query = """
        query($owner: String!, $repo: String!, $number: Int!) {
          repository(owner: $owner, name: $repo) {
            issue(number: $number) {
              comments(last: 10) {
                nodes {
                  author { login }
                  body
                  createdAt
                }
              }
            }
          }
        }
        """
        data = self.graphql(query, {"owner": self.owner, "repo": self.repo, "number": number})
        comments = (
            data.get("data", {})
            .get("repository", {})
            .get("issue", {})
            .get("comments", {})
            .get("nodes", [])
        )
        last_claim = None
        for comment in comments:
            body = comment.get("body") or ""
            if "[yt-claim]" in body:
                last_claim = comment
        if not last_claim:
            return None
        run_id = _extract_claim_field(last_claim.get("body") or "", "run_id")
        return {
            "author": (last_claim.get("author") or {}).get("login"),
            "body": last_claim.get("body") or "",
            "run_id": run_id,
            "created_at": last_claim.get("createdAt"),
        }

    def create_pr(self, title, body, head):
        """Create a PR and return the URL."""
        result = self.shell.run(
            [
                "gh",
                "pr",
                "create",
                "-R",
                self.repo_slug(),
                "--base",
                "main",
                "--head",
                head,
                "--title",
                title,
                "--body",
                body,
            ]
        )
        return _extract_first_url(result.stdout)

    def move_issue_status(self, number, status):
        """Move an issue to a project status."""
        cache = self._ensure_project_cache()
        item = self.get_issue_project_item(number, cache["project_id"])
        option_id = cache["status_options"].get(status)
        if not option_id:
            raise RuntimeError(f"Unknown status option: {status}")

        mutation = """
        mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
          updateProjectV2ItemFieldValue(
            input: {
              projectId: $project,
              itemId: $item,
              fieldId: $field,
              value: { singleSelectOptionId: $option }
            }
          ) {
            projectV2Item { id }
          }
        }
        """
        self.graphql(
            mutation,
            {
                "project": cache["project_id"],
                "item": item["item_id"],
                "field": cache["status_field_id"],
                "option": option_id,
            },
        )

    def get_issue_project_item(self, number, project_id):
        """Return project item id and status for an issue."""
        query = """
        query($owner: String!, $repo: String!, $number: Int!) {
          repository(owner: $owner, name: $repo) {
            issue(number: $number) {
              state
              projectItems(first: 20) {
                nodes {
                  id
                  project { id }
                  fieldValues(first: 20) {
                    nodes {
                      ... on ProjectV2ItemFieldSingleSelectValue {
                        field {
                          ... on ProjectV2SingleSelectField { name }
                        }
                        name
                        optionId
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """
        data = self.graphql(query, {"owner": self.owner, "repo": self.repo, "number": number})
        issue = data.get("data", {}).get("repository", {}).get("issue")
        if not issue:
            raise RuntimeError(f"Issue {number} not found")
        for item in issue.get("projectItems", {}).get("nodes", []) or []:
            project = item.get("project") or {}
            if project.get("id") != project_id:
                continue
            status = None
            for field in item.get("fieldValues", {}).get("nodes", []) or []:
                field_name = ((field.get("field") or {}).get("name"))
                if field_name == "Status":
                    status = field.get("name")
            if not status:
                raise RuntimeError(f"Issue {number} missing Status field")
            return {"item_id": item.get("id"), "status": status}
        raise RuntimeError(f"Issue {number} not in project")

    def list_ready_issues(self, label):
        """Return issues in ready status ordered by createdAt."""
        cache = self._ensure_project_cache()
        issues = self.list_open_issues(label)
        issues = sorted(issues, key=lambda item: item.get("createdAt") or "")
        ready = []
        for issue in issues:
            item = self.get_issue_project_item(issue["number"], cache["project_id"])
            if item["status"] == "ready":
                ready.append(issue)
        return ready

    def graphql(self, query, variables):
        """Run a GraphQL query via gh api graphql."""
        cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            if value is None:
                cmd.extend(["-F", f"{key}=null"])
            else:
                cmd.extend(["-F", f"{key}={value}"])
        return self.shell.run_json(cmd)

    def _ensure_project_cache(self):
        if self._project_cache:
            return self._project_cache
        if self.project_number:
            data = self._fetch_project_by_number(self.project_number)
        else:
            if not self.project_title:
                raise RuntimeError("Missing project number or title")
            data = self._fetch_project_by_title(self.project_title)
        self._project_cache = data
        return data

    def _fetch_project_by_number(self, number):
        query = """
        query($owner: String!, $number: Int!) {
          organization(login: $owner) {
            projectV2(number: $number) {
              id
              title
              fields(first: 50) {
                nodes {
                  ... on ProjectV2SingleSelectField {
                    id
                    name
                    options { id name }
                  }
                }
              }
            }
          }
        }
        """
        data = self.graphql(query, {"owner": self.owner, "number": number})
        project = data.get("data", {}).get("organization", {}).get("projectV2")
        if not project:
            raise RuntimeError("Project not found")
        return _project_cache_from_data(project)

    def _fetch_project_by_title(self, title):
        query = """
        query($owner: String!) {
          organization(login: $owner) {
            projectsV2(first: 50) {
              nodes { id title }
            }
          }
        }
        """
        data = self.graphql(query, {"owner": self.owner})
        projects = data.get("data", {}).get("organization", {}).get("projectsV2", {}).get("nodes", [])
        project_id = None
        for project in projects:
            if project.get("title") == title:
                project_id = project.get("id")
        if not project_id:
            raise RuntimeError("Project title not found")
        query = """
        query($project: ID!) {
          node(id: $project) {
            ... on ProjectV2 {
              id
              title
              fields(first: 50) {
                nodes {
                  ... on ProjectV2SingleSelectField {
                    id
                    name
                    options { id name }
                  }
                }
              }
            }
          }
        }
        """
        data = self.graphql(query, {"project": project_id})
        project = data.get("data", {}).get("node")
        if not project:
            raise RuntimeError("Project node not found")
        return _project_cache_from_data(project)


def _project_cache_from_data(project):
    status_field = None
    for field in project.get("fields", {}).get("nodes", []) or []:
        if field.get("name") == "Status":
            status_field = field
    if not status_field:
        raise RuntimeError("Status field not found in project")
    options = {option["name"]: option["id"] for option in status_field.get("options", [])}
    return {
        "project_id": project.get("id"),
        "status_field_id": status_field.get("id"),
        "status_options": options,
    }


def _extract_first_url(text):
    if not text:
        return ""
    for token in text.split():
        if token.startswith("https://"):
            return token
    return ""


def _extract_claim_field(body, field):
    if not body:
        return ""
    prefix = f"{field}:"
    for line in body.splitlines():
        if line.strip().startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""
