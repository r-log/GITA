"""
ORM models — import all here so Alembic and Base.metadata can discover them.
"""

from src.models.repository import Repository
from src.models.milestone import MilestoneModel
from src.models.issue import IssueModel
from src.models.pull_request import PullRequestModel
from src.models.smart_evaluation import SmartEvaluationModel
from src.models.analysis import Analysis
from src.models.agent_run import AgentRun
from src.models.onboarding_run import OnboardingRun
from src.models.file_mapping import FileMapping
from src.models.code_index import CodeIndex
from src.models.graph_node import GraphNode
from src.models.graph_edge import GraphEdge
from src.models.pr_file_change import PrFileChange
from src.models.event import EventModel
from src.models.commit import CommitModel
from src.models.comment import CommentModel
from src.models.review import ReviewModel
from src.models.diff import DiffModel

__all__ = [
    "Repository",
    "MilestoneModel",
    "IssueModel",
    "PullRequestModel",
    "SmartEvaluationModel",
    "Analysis",
    "AgentRun",
    "OnboardingRun",
    "FileMapping",
    "CodeIndex",
    "GraphNode",
    "GraphEdge",
    "PrFileChange",
    "EventModel",
    "CommitModel",
    "CommentModel",
    "ReviewModel",
    "DiffModel",
]
