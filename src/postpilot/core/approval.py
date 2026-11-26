from postpilot.core.states import IllegalTransition
from postpilot.projects import Project, Projects


class ProjectApprovalError(IllegalTransition):
    pass


def enabled_project(projects: Projects, key: str) -> Project:
    project = projects.get(key)
    if project is None:
        raise ProjectApprovalError(f"Unknown project {key!r}")
    if not project.enabled:
        raise ProjectApprovalError(f"Project {key!r} is disabled")
    return project


def auto_approved_project(projects: Projects, key: str) -> Project:
    project = enabled_project(projects, key)
    if project.approval != "auto":
        raise ProjectApprovalError(f"Project {key!r} needs a tap")
    return project
