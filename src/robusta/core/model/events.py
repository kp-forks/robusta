import copy
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Callable

from pydantic import BaseModel

from robusta.core.pubsub.event_emitter import EventEmitter
from robusta.core.reporting.base import (
    BaseBlock,
    EnrichmentType,
    Finding,
    FindingSeverity,
    FindingSource,
    FindingSubject,
    FindingSubjectType,
    Link,
    LinkType,
)
from robusta.core.sinks import SinkBase
from robusta.integrations.scheduled.playbook_scheduler import PlaybooksScheduler


class EventType(Enum):
    KUBERNETES_TOPOLOGY_CHANGE = 1
    PROMETHEUS = 2
    MANUAL_TRIGGER = 3
    SCHEDULED_TRIGGER = 4


class ExecutionEventBaseParams(BaseModel):
    named_sinks: Optional[List[str]] = None


class ExecutionContext(BaseModel):
    account_id: str
    cluster_name: str


# Right now:
# 1. this is a dataclass but we need to make all fields optional in subclasses because of https://stackoverflow.com/questions/51575931/
# 2. this can't be a pydantic BaseModel because of various pydantic bugs (see https://github.com/samuelcolvin/pydantic/pull/2557)
# once the pydantic PR that addresses those issues is merged, this should be a pydantic class
# (note that we need to integrate with dataclasses because of hikaru)
@dataclass
class ExecutionBaseEvent:
    # Collection of findings that should be sent to each sink.
    # This collection is shared between different playbooks that are triggered by the same event.
    sink_findings: Dict[str, List[Finding]] = field(default_factory=lambda: defaultdict(list))
    # Target sinks for this execution event. Each playbook may have a different list of target sinks.
    named_sinks: Optional[List[str]] = None
    all_sinks: Optional[Dict[str, SinkBase]] = None
    #  Response returned to caller. For admission or manual triggers for example
    response: Dict[str, Any] = None  # type: ignore
    stop_processing: bool = False
    _scheduler: Optional[PlaybooksScheduler] = None
    _context: Optional[ExecutionContext] = None
    _event_emitter: Optional[EventEmitter] = None
    _ws: Optional[Callable[[str], None]] = None

    def set_context(self, context: ExecutionContext):
        self._context = context

    def get_context(self) -> ExecutionContext:
        return self._context

    def set_scheduler(self, scheduler: PlaybooksScheduler):
        self._scheduler = scheduler

    def get_scheduler(self) -> PlaybooksScheduler:
        return self._scheduler

    def set_event_emitter(self, emitter: EventEmitter):
        self._event_emitter = emitter

    def create_default_finding(self) -> Finding:
        """Create finding default fields according to the event type"""
        return Finding(title="Robusta notification", aggregation_key="GenericFindingKey")

    def set_all_sinks(self, all_sinks: Dict[str, SinkBase]):
        self.all_sinks = all_sinks

    def get_all_sinks(self):
        return self.all_sinks

    def is_sink_findings_empty(self) -> bool:
        return len(self.sink_findings) == 0

    def __prepare_sinks_findings(self):
        finding_id: uuid.UUID = uuid.uuid4()
        for sink in self.named_sinks:
            if len(self.sink_findings[sink]) == 0:
                sink_finding = self.create_default_finding()
                sink_finding.id = finding_id  # share the same finding id between different sinks
                self.sink_findings[sink].append(sink_finding)

    def add_link(self, link: Link, suppress_warning: bool = False) -> None:
        self.__prepare_sinks_findings()
        for sink in self.named_sinks:
            self.sink_findings[sink][0].add_link(link, suppress_warning)

    def add_video_link(self, video_link: Link) -> None:
        # For backward compatability
        video_link.type = LinkType.VIDEO
        self.add_link(video_link, True)

    def emit_event(self, event_name: str, **kwargs):
        """Publish an event to the pubsub. It will be processed by the sinks during the execution of the playbook."""

        if self._event_emitter:
            self._event_emitter.emit_event(event_name, **kwargs)

    def add_enrichment(
        self,
        enrichment_blocks: List[BaseBlock],
        annotations=None,
        enrichment_type: Optional[EnrichmentType] = None,
        title: Optional[str] = None,
    ):
        self.__prepare_sinks_findings()
        for sink in self.named_sinks:
            self.sink_findings[sink][0].add_enrichment(
                enrichment_blocks, annotations, True, enrichment_type=enrichment_type, title=title
            )

    def add_finding(self, finding: Finding, suppress_warning: bool = False):
        finding.dirty = True  # Warn if new enrichments are added to this finding directly
        first = True  # no need to clone the finding on the first sink. Use the orig finding
        for sink in self.named_sinks:
            if (len(self.sink_findings[sink]) > 0) and not suppress_warning:
                logging.warning(f"Overriding active finding for {sink}. new finding: {finding}")
            if not first:
                finding = copy.deepcopy(finding)
            self.sink_findings[sink].insert(0, finding)
            first = False

    def override_finding_attributes(
        self,
        title: Optional[str] = None,
        description: Optional[str] = None,
        severity: FindingSeverity = None,
        aggregation_key: Optional[str] = None,
    ):
        for sink in self.named_sinks:
            for finding in self.sink_findings[sink]:
                if title:
                    finding.title = title
                if description:
                    finding.description = description
                if severity:
                    finding.severity = severity
                if aggregation_key:
                    finding.aggregation_key = aggregation_key

    def extend_description(self, text: str):
        for sink in self.named_sinks:
            for finding in self.sink_findings[sink]:
                if not finding.description:
                    finding.description = text
                else:
                    finding.description += f"\n\n{text}"

    @staticmethod
    def from_params(params: ExecutionEventBaseParams) -> Optional["ExecutionBaseEvent"]:
        return ExecutionBaseEvent(named_sinks=params.named_sinks)

    def get_subject(self) -> FindingSubject:
        return FindingSubject(name="Unresolved", subject_type=FindingSubjectType.TYPE_NONE)

    @classmethod
    def get_source(cls) -> FindingSource:
        return FindingSource.NONE
