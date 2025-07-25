import logging
import os
import threading
import time
from collections import defaultdict
from concurrent.futures.process import BrokenProcessPool, ProcessPoolExecutor
from typing import Dict, List, Optional, Union
import dpath.util
import prometheus_client
from hikaru.model.rel_1_26 import (
    Container,
    DaemonSet,
    Deployment,
    Job,
    ObjectMeta,
    Pod,
    ReplicaSet,
    StatefulSet,
    Volume,
)
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes.client import (
    V1Container,
    V1DaemonSet,
    V1DaemonSetList,
    V1Deployment,
    V1DeploymentList,
    V1Job,
    V1JobList,
    V1NodeList,
    V1ObjectMeta,
    V1Pod,
    V1PodList,
    V1PodTemplateSpec,
    V1ReplicaSetList,
    V1StatefulSet,
    V1StatefulSetList,
    V1Volume,
)
from pydantic import BaseModel

from robusta.core.discovery import utils
from robusta.core.model.cluster_status import ClusterStats
from robusta.core.model.env_vars import (
    ARGO_ROLLOUTS,
    DISABLE_HELM_MONITORING,
    DISCOVERY_BATCH_SIZE,
    DISCOVERY_MAX_BATCHES,
    DISCOVERY_POD_OWNED_PODS,
    DISCOVERY_PROCESS_TIMEOUT_SEC,
    IS_OPENSHIFT,
    OPENSHIFT_GROUPS,
    CUSTOM_CRD
)
from robusta.core.model.helm_release import HelmRelease
from robusta.core.model.jobs import JobInfo
from robusta.core.model.namespaces import NamespaceInfo
from robusta.core.model.nodes import NodeInfo
from robusta.core.model.openshift_group import OpenshiftGroup
from robusta.core.model.services import ContainerInfo, ServiceConfig, ServiceInfo, VolumeInfo
from robusta.integrations.kubernetes.custom_models import DeploymentConfig, DictToK8sObj, Rollout
from robusta.integrations.kubernetes.custom_crds import CRDS_map
from robusta.patch.patch import create_monkey_patches
from robusta.utils.cluster_provider_discovery import cluster_provider
from robusta.utils.stack_tracer import StackTracer

discovery_errors_count = prometheus_client.Counter("discovery_errors", "Number of discovery process failures.")
discovery_process_time = prometheus_client.Summary(
    "discovery_process_time",
    "Total discovery process time (seconds)",
)


class DiscoveryResults(BaseModel):
    services: List[ServiceInfo] = []
    nodes: List[NodeInfo] = None
    node_requests: Dict = {}
    jobs: List[JobInfo] = []
    namespaces: List[NamespaceInfo] = []
    helm_releases: List[HelmRelease] = []
    pods_running_count: int = 0
    openshift_groups: List[OpenshiftGroup] = []

    class Config:
        arbitrary_types_allowed = True


DISCOVERY_STACKTRACE_FILE = "/tmp/make_discovery_stacktrace"
DISCOVERY_STACKTRACE_TIMEOUT_S = int(os.environ.get("DISCOVERY_STACKTRACE_TIMEOUT_S", 10))
KIND_TO_COREV1_METHOD = {
    "pods": "list_pod_for_all_namespaces",
    "configmaps": "list_config_map_for_all_namespaces",
    "endpoints": "list_endpoints_for_all_namespaces",
    "services": "list_service_for_all_namespaces",
    "secrets": "list_secret_for_all_namespaces",
    "persistentvolumeclaims": "list_persistent_volume_claim_for_all_namespaces",
    "serviceaccounts": "list_service_account_for_all_namespaces",
    "replicationcontrollers": "list_replication_controller_for_all_namespaces",
    "limitranges": "list_limit_range_for_all_namespaces",
    "resourcequotas": "list_resource_quota_for_all_namespaces",
    "events": "list_event_for_all_namespaces",
    "podtemplates": "list_pod_template_for_all_namespaces",
}

class ResourceAccessForbiddenError(Exception):
    """Raised when access to a Kubernetes resource is forbidden (HTTP 403)."""
    pass

class Discovery:
    executor = ProcessPoolExecutor(max_workers=1)  # always 1 discovery process
    stacktrace_thread_active = False
    out_of_memory_detected = False

    @staticmethod
    def create_stacktrace():
        try:
            with open(DISCOVERY_STACKTRACE_FILE, "x"):
                logging.info("Sending signal to discovery thread")
        except Exception:
            logging.error("error creating stack trace", exc_info=True)

    @staticmethod
    def stack_dump_on_signal():
        try:
            while Discovery.stacktrace_thread_active:
                if os.path.exists(DISCOVERY_STACKTRACE_FILE):
                    logging.info("discovery process stack trace")
                    StackTracer.dump()
                    return
                time.sleep(DISCOVERY_STACKTRACE_TIMEOUT_S)
        except Exception:
            logging.error("error getting stack trace", exc_info=True)

    @staticmethod
    def __create_service_info_from_hikaru(
        meta: ObjectMeta,
        kind: str,
        containers: List[Container],
        volumes: List[Volume],
        total_pods: int,
        ready_pods: int,
        is_helm_release: bool = False,
    ) -> ServiceInfo:
        container_info = (
            [ContainerInfo.get_container_info_hikaru(container) for container in containers] if containers else []
        )
        volumes_info = [VolumeInfo.get_volume_info(volume) for volume in volumes] if volumes else []
        config = ServiceConfig(labels=meta.labels or {}, containers=container_info, volumes=volumes_info)
        version = getattr(meta, "resource_version", None) or getattr(meta, "resourceVersion", None)
        resource_version = int(version) if version else 0

        return ServiceInfo(
            resource_version=resource_version,
            name=meta.name,
            namespace=meta.namespace,
            service_type=kind,
            service_config=config,
            ready_pods=ready_pods,
            total_pods=total_pods,
            is_helm_release=is_helm_release,
        )

    @staticmethod
    def __create_service_info(
        meta: V1ObjectMeta,
        kind: str,
        containers: List[V1Container],
        volumes: List[V1Volume],
        total_pods: int,
        ready_pods: int,
        is_helm_release: bool = False,
    ) -> ServiceInfo:
        container_info = [ContainerInfo.get_container_info(container) for container in containers] if containers else []
        volumes_info = [VolumeInfo.get_volume_info(volume) for volume in volumes] if volumes else []
        config = ServiceConfig(labels=meta.labels or {}, containers=container_info, volumes=volumes_info)
        version = getattr(meta, "resource_version", None) or getattr(meta, "resourceVersion", None)
        resource_version = int(version) if version else 0

        return ServiceInfo(
            resource_version=resource_version,
            name=meta.name,
            namespace=meta.namespace,
            service_type=kind,
            service_config=config,
            ready_pods=ready_pods,
            total_pods=total_pods,
            is_helm_release=is_helm_release,
        )

    @staticmethod
    def create_service_info_from_hikaru(obj: Union[Deployment, DaemonSet, StatefulSet, Pod, ReplicaSet]) -> ServiceInfo:
        return Discovery.__create_service_info_from_hikaru(
            obj.metadata,
            obj.kind,
            extract_containers_k8(obj),
            extract_volumes_k8(obj),
            extract_total_pods(obj),
            extract_ready_pods(obj),
            is_helm_release=is_release_managed_by_helm(
                annotations=obj.metadata.annotations, labels=obj.metadata.labels
            ),
        )

    @staticmethod
    def count_resources(kind, api_group, version):
        if not api_group:
            items = Discovery._fetch_corev1_resources(kind, version)
        else:
            items = Discovery._fetch_crd_resources(kind, api_group, version)

        return Discovery._count_items_by_namespace(items, kind)


    @staticmethod
    def _fetch_corev1_resources(kind, version):
        if version != "v1":
            logging.error(f"Unsupported CoreV1 resource version '{version}' for kind '{kind}'")
            return []

        method_name = KIND_TO_COREV1_METHOD.get(kind.lower())
        if not method_name:
            logging.warning(f"No CoreV1Api mapping for kind: '{kind}'")
            return []

        core_v1 = client.CoreV1Api()
        method = getattr(core_v1, method_name, None)
        if not method:
            logging.warning(f"CoreV1Api does not have method '{method_name}' for kind '{kind}'")
            return []

        try:
            response = method()
            return getattr(response, 'items', response)
        except ApiException as e:
            if e.status == 403:
                raise ResourceAccessForbiddenError(f"Access forbidden to CoreV1 resource '{kind}': {e.body}")
            raise


    @staticmethod
    def _fetch_crd_resources(kind, api_group, version):
        items = []
        continue_ref = None
        for _ in range(DISCOVERY_MAX_BATCHES):
            try:
                crd_res = client.CustomObjectsApi().list_cluster_custom_object(
                    group=api_group,
                    version=version,
                    plural=kind.lower(),
                    limit=DISCOVERY_BATCH_SIZE,
                    _continue=continue_ref,
                )
                items.extend(crd_res.get("items", []))
                continue_ref = crd_res.get("metadata", {}).get("continue")
                if not continue_ref:
                    break
            except ApiException as e:
                if e.status == 403:
                    raise ResourceAccessForbiddenError(
                        f"Access forbidden to resource '{kind}' in group '{api_group}': {e.body}"
                    )
                logging.exception(f"Failed to list {kind} from api group '{api_group}'.")
                break
        return items


    @staticmethod
    def _count_items_by_namespace(items, kind):
        namespace_counts = defaultdict(int)
        for item in items:
            metadata = getattr(item, 'metadata', None)
            namespace = None

            if metadata:
                namespace = getattr(metadata, 'namespace', None)
            elif isinstance(item, dict):
                metadata = item.get('metadata', {})
                namespace = metadata.get('namespace')

            if not namespace:
                logging.warning(f"Missing namespace for resource '{kind}': metadata={metadata}")
                continue

            namespace_counts[namespace] += 1

        return dict(namespace_counts)


    @staticmethod
    def discovery_process() -> DiscoveryResults:
        create_monkey_patches()
        Discovery.stacktrace_thread_active = True
        threading.Thread(target=Discovery.stack_dump_on_signal, daemon=True).start()
        pods_metadata: List[V1ObjectMeta] = []
        node_requests = defaultdict(list)  # map between node name, to request of pods running on it
        active_services: List[ServiceInfo] = []
        openshift_groups: List[OpenshiftGroup] = []
        continue_ref: Optional[str] = None
        # discover micro services

        try:
            for cls_name in CUSTOM_CRD:
                if (cls := CRDS_map.get(cls_name)) is None:
                    continue

                for _ in range(DISCOVERY_MAX_BATCHES):
                    try:
                        crd_res = client.CustomObjectsApi().list_cluster_custom_object(
                            group=cls.group,
                            version=cls.version,
                            plural=cls.plural,
                            limit=DISCOVERY_BATCH_SIZE,
                            _continue=continue_ref,
                        )
                    except Exception:
                        logging.exception(msg=f"Failed to list {cls.name} from api.")
                        break

                    for crd in crd_res.get("items", []):
                        try:
                            meta = DictToK8sObj(crd.get("metadata"), V1ObjectMeta)
                            active_services.extend(
                                [
                                    Discovery.__create_service_info(
                                        meta=meta,
                                        kind=cls.name,
                                        containers=[],
                                        volumes=[],
                                        total_pods=dpath.util.get(crd, cls.total_pods_path, default=0),
                                        ready_pods=dpath.util.get(crd, cls.ready_pods_path, default=0),
                                        is_helm_release=is_release_managed_by_helm(
                                            annotations=meta.annotations, labels=meta.labels
                                        ),
                                    )
                                ]
                            )
                        except Exception:
                            logging.exception(msg=f"Failed to parse {cls.name} {crd}")
                            continue

                    continue_ref = crd_res.get("metadata", {}).get("continue")
                    if not continue_ref:
                        break

            if IS_OPENSHIFT:
                for _ in range(DISCOVERY_MAX_BATCHES):
                    try:
                        deployconfigs_res = client.CustomObjectsApi().list_cluster_custom_object(
                            group=DeploymentConfig.group,
                            version=DeploymentConfig.version,
                            plural=DeploymentConfig.plural,
                            limit=DISCOVERY_BATCH_SIZE,
                            _continue=continue_ref,
                        )
                    except Exception:
                        logging.exception(msg="Failed to list Deployment configs from api.")
                        break

                    for dc in deployconfigs_res.get("items", []):
                        try:
                            meta = DictToK8sObj(dc.get("metadata"), V1ObjectMeta)
                            spec = dc.get("spec", {})
                            template = DictToK8sObj(spec.get("template"), V1PodTemplateSpec)

                            active_services.extend(
                                [
                                    Discovery.__create_service_info(
                                        meta=meta,
                                        kind="DeploymentConfig",
                                        containers=template.spec.containers,
                                        volumes=template.spec.volumes,
                                        total_pods=spec.get("replicas", 1),
                                        ready_pods=dc.get("status", {}).get("readyReplicas", 0),
                                        is_helm_release=is_release_managed_by_helm(
                                            annotations=meta.annotations, labels=meta.labels
                                        ),
                                    )
                                ]
                            )
                        except Exception:
                            logging.exception(msg=f"Failed to parse Deployment config/n {dc}")
                            continue

                    continue_ref = deployconfigs_res.get("metadata", {}).get("continue")
                    if not continue_ref:
                        break

            if OPENSHIFT_GROUPS:
                groupname_to_namespaces = defaultdict(list)
                try:
                    role_bindings = client.RbacAuthorizationV1Api().list_role_binding_for_all_namespaces()
                    for role_binding in role_bindings.items:
                        ns = role_binding.metadata.namespace

                        if not role_binding.subjects:
                            logging.info(f"Skipping role binding: {role_binding.metadata.name} in ns: {role_binding.metadata.namespace}")
                            continue

                        for subject in role_binding.subjects:
                            if subject.kind == "Group":
                                groupname_to_namespaces[subject.name].append(ns)

                except Exception:
                    logging.exception(msg="Failed to build Openshift rolebinding to groups map.")

                for _ in range(DISCOVERY_MAX_BATCHES):
                    try:
                        os_groups = client.CustomObjectsApi().list_cluster_custom_object(
                            group="user.openshift.io",
                            version="v1",
                            plural="groups",
                            limit=DISCOVERY_BATCH_SIZE,
                            _continue=continue_ref,
                        )
                    except Exception:
                        logging.exception(msg="Failed to list Openshift groups from api.")
                        break

                    for os_group in os_groups.get("items", []):
                        try:
                            meta = os_group.get("metadata", {})
                            name = meta.get("name")
                            openshift_groups.extend(
                                [
                                    OpenshiftGroup(
                                        name=name,
                                        users=os_group.get("users", []) or [],
                                        namespaces=groupname_to_namespaces.get(name, []),
                                        labels=meta.get("labels"),
                                        annotations=meta.get("annotations"),
                                        resource_version=meta.get("resourceVersion"),
                                    )
                                ]
                            )
                        except Exception:
                            logging.exception(msg=f"Failed to parse Openshift Group/n {os_group}")
                            continue

                    continue_ref = os_groups.get("metadata", {}).get("continue")
                    if not continue_ref:
                        break
            # rollouts.
            continue_ref = None
            if ARGO_ROLLOUTS:
                for _ in range(DISCOVERY_MAX_BATCHES):
                    try:
                        rollouts_res = client.CustomObjectsApi().list_cluster_custom_object(
                            group=Rollout.group,
                            version=Rollout.version,
                            plural=Rollout.plural,
                            limit=DISCOVERY_BATCH_SIZE,
                            _continue=continue_ref,
                        )
                    except Exception:
                        logging.exception(msg="Failed to list Argo Rollouts from api.")
                        break

                    for ro in rollouts_res.get("items", []):
                        try:
                            meta = DictToK8sObj(ro.get("metadata"), V1ObjectMeta)
                            spec = ro.get("spec", {})
                            template = DictToK8sObj(spec.get("template"), V1PodTemplateSpec)
                            status = ro.get("status", {})

                            active_services.extend(
                                [
                                    Discovery.__create_service_info(
                                        meta=meta,
                                        kind=Rollout.kind,
                                        containers=template.spec.containers if template else [],
                                        volumes=template.spec.volumes if template else [],
                                        total_pods=status.get("replicas", 1),
                                        ready_pods=status.get("readyReplicas", 0),
                                        is_helm_release=is_release_managed_by_helm(
                                            annotations=meta.annotations, labels=meta.labels
                                        ),
                                    )
                                ]
                            )
                        except Exception:
                            logging.exception(msg=f"Failed to parse Rollout/n {ro}")
                            continue

                    continue_ref = rollouts_res.get("metadata", {}).get("continue")
                    if not continue_ref:
                        break

            # discover deployments
            # using k8s api `continue` to load in batches
            continue_ref = None
            for _ in range(DISCOVERY_MAX_BATCHES):
                deployments: V1DeploymentList = client.AppsV1Api().list_deployment_for_all_namespaces(
                    limit=DISCOVERY_BATCH_SIZE, _continue=continue_ref
                )
                active_services.extend(
                    [
                        Discovery.__create_service_info(
                            deployment.metadata,
                            "Deployment",
                            extract_containers(deployment),
                            extract_volumes(deployment),
                            extract_total_pods(deployment),
                            extract_ready_pods(deployment),
                            is_helm_release=is_release_managed_by_helm(
                                annotations=deployment.metadata.annotations, labels=deployment.metadata.labels
                            ),
                        )
                        for deployment in deployments.items
                    ]
                )
                continue_ref = deployments.metadata._continue
                if not continue_ref:
                    break

            # discover statefulsets
            continue_ref = None
            for _ in range(DISCOVERY_MAX_BATCHES):
                statefulsets: V1StatefulSetList = client.AppsV1Api().list_stateful_set_for_all_namespaces(
                    limit=DISCOVERY_BATCH_SIZE, _continue=continue_ref
                )
                active_services.extend(
                    [
                        Discovery.__create_service_info(
                            statefulset.metadata,
                            "StatefulSet",
                            extract_containers(statefulset),
                            extract_volumes(statefulset),
                            extract_total_pods(statefulset),
                            extract_ready_pods(statefulset),
                            is_helm_release=is_release_managed_by_helm(
                                annotations=statefulset.metadata.annotations, labels=statefulset.metadata.labels
                            ),
                        )
                        for statefulset in statefulsets.items
                    ]
                )
                continue_ref = statefulsets.metadata._continue
                if not continue_ref:
                    break

            # discover daemonsets
            continue_ref = None
            for _ in range(DISCOVERY_MAX_BATCHES):
                daemonsets: V1DaemonSetList = client.AppsV1Api().list_daemon_set_for_all_namespaces(
                    limit=DISCOVERY_BATCH_SIZE, _continue=continue_ref
                )
                active_services.extend(
                    [
                        Discovery.__create_service_info(
                            daemonset.metadata,
                            "DaemonSet",
                            extract_containers(daemonset),
                            extract_volumes(daemonset),
                            extract_total_pods(daemonset),
                            extract_ready_pods(daemonset),
                            is_helm_release=is_release_managed_by_helm(
                                annotations=daemonset.metadata.annotations, labels=daemonset.metadata.labels
                            ),
                        )
                        for daemonset in daemonsets.items
                    ]
                )
                continue_ref = daemonsets.metadata._continue
                if not continue_ref:
                    break

            # discover replicasets
            continue_ref = None
            for _ in range(DISCOVERY_MAX_BATCHES):
                replicasets: V1ReplicaSetList = client.AppsV1Api().list_replica_set_for_all_namespaces(
                    limit=DISCOVERY_BATCH_SIZE, _continue=continue_ref
                )
                active_services.extend(
                    [
                        Discovery.__create_service_info(
                            replicaset.metadata,
                            "ReplicaSet",
                            extract_containers(replicaset),
                            extract_volumes(replicaset),
                            extract_total_pods(replicaset),
                            extract_ready_pods(replicaset),
                            is_helm_release=is_release_managed_by_helm(
                                annotations=replicaset.metadata.annotations, labels=replicaset.metadata.labels
                            ),
                        )
                        for replicaset in replicasets.items
                        if not replicaset.metadata.owner_references and replicaset.spec.replicas > 0
                    ]
                )
                continue_ref = replicasets.metadata._continue
                if not continue_ref:
                    break

            # discover pods
            continue_ref = None
            pods_running_count = 0
            for _ in range(DISCOVERY_MAX_BATCHES):
                pods: V1PodList = client.CoreV1Api().list_pod_for_all_namespaces(
                    limit=DISCOVERY_BATCH_SIZE, _continue=continue_ref
                )
                for pod in pods.items:
                    pods_metadata.append(pod.metadata)
                    if should_report_pod(pod):
                        active_services.append(
                            Discovery.__create_service_info(
                                pod.metadata,
                                "Pod",
                                extract_containers(pod),
                                extract_volumes(pod),
                                extract_total_pods(pod),
                                extract_ready_pods(pod),
                                is_helm_release=is_release_managed_by_helm(
                                    annotations=pod.metadata.annotations, labels=pod.metadata.labels
                                ),
                            )
                        )

                    pod_status = pod.status.phase
                    if pod_status in ["Running", "Unknown", "Pending"] and pod.spec.node_name:
                        node_requests[pod.spec.node_name].append(utils.k8s_pod_requests(pod))
                    if pod_status == "Running":
                        pods_running_count += 1

                continue_ref = pods.metadata._continue
                if not continue_ref:
                    break

        except Exception as e:
            logging.error(
                "Failed to run periodic service discovery",
                exc_info=True,
            )
            raise e

        # discover nodes - no need for batching. Number of nodes is not big enough
        try:
            current_nodes: V1NodeList = client.CoreV1Api().list_node()
            nodes = [utils.from_api_server_node(node, node_requests.get(node.metadata.name, [])) for node in current_nodes.items]
        except Exception as e:
            logging.error(
                "Failed to run periodic nodes discovery",
                exc_info=True,
            )
            raise e

        # discover jobs
        active_jobs: List[JobInfo] = []
        try:
            continue_ref: Optional[str] = None
            for _ in range(DISCOVERY_MAX_BATCHES):
                current_jobs: V1JobList = client.BatchV1Api().list_job_for_all_namespaces(
                    limit=DISCOVERY_BATCH_SIZE, _continue=continue_ref
                )
                for job in current_jobs.items:
                    job_pods = []
                    job_labels = {}
                    if job.spec.selector:
                        job_labels = job.spec.selector.match_labels
                    elif job.metadata.labels:
                        job_name = job.metadata.labels.get("job-name", None)
                        if job_name:
                            job_labels = {"job-name": job_name}

                    if job_labels:  # add job pods only if we found a valid selector
                        job_pods = [
                            pod_meta.name
                            for pod_meta in pods_metadata
                            if (
                                (job.metadata.namespace == pod_meta.namespace)
                                and (job_labels.items() <= (pod_meta.labels or {}).items())
                            )
                        ]

                    active_jobs.append(JobInfo.from_api_server(job, job_pods))

                continue_ref = current_jobs.metadata._continue
                if not continue_ref:
                    break

        except Exception as e:
            logging.error(
                "Failed to run periodic jobs discovery",
                exc_info=True,
            )
            raise e

        helm_releases_map: dict[str, HelmRelease] = {}
        if not DISABLE_HELM_MONITORING:
            # discover helm state
            try:
                continue_ref: Optional[str] = None
                for _ in range(DISCOVERY_MAX_BATCHES):
                    secrets = client.CoreV1Api().list_secret_for_all_namespaces(
                        label_selector="owner=helm", _continue=continue_ref
                    )
                    if not secrets.items:
                        break

                    for secret_item in secrets.items:
                        release_data = secret_item.data.get("release", None)
                        if not release_data:
                            continue

                        try:
                            decoded_release_row = HelmRelease.from_api_server(secret_item.data["release"])
                            # we use map here to deduplicate and pick only the latest release data
                            helm_releases_map[decoded_release_row.get_service_key()] = decoded_release_row
                        except Exception as e:
                            logging.error(f"an error occurred while decoding helm releases: {e}")

                    continue_ref = secrets.metadata._continue
                    if not continue_ref:
                        break

            except Exception as e:
                logging.error(
                    "Failed to run periodic helm discovery",
                    exc_info=True,
                )
                raise e

        # discover namespaces
        try:
            namespaces: List[NamespaceInfo] = [
                NamespaceInfo.from_api_server(namespace) for namespace in client.CoreV1Api().list_namespace().items
            ]
        except Exception as e:
            logging.error(
                "Failed to run periodic namespaces discovery",
                exc_info=True,
            )
            raise e
        Discovery.stacktrace_thread_active = False

        return DiscoveryResults(
            services=active_services,
            nodes=nodes,
            node_requests=node_requests,
            jobs=active_jobs,
            namespaces=namespaces,
            helm_releases=list(helm_releases_map.values()),
            pods_running_count=pods_running_count,
            openshift_groups=openshift_groups,
        )

    @staticmethod
    @discovery_errors_count.count_exceptions()
    @discovery_process_time.time()
    def discover_resources() -> DiscoveryResults:
        try:
            future = Discovery.executor.submit(Discovery.discovery_process)
            return future.result(timeout=DISCOVERY_PROCESS_TIMEOUT_SEC)
        except Exception as e:
            # We've seen this and believe the process is killed due to oom kill
            # The process pool becomes not usable, so re-creating it
            logging.error("Discovery process internal error")
            if isinstance(e, BrokenProcessPool):
                Discovery.out_of_memory_detected = True
                logging.error("The discovery process was killed, likely due to an Out of Memory error. Refer to the following documentation to increase the available memory for the pod robusta-runner: https://docs.robusta.dev/master/help.html")

            Discovery.executor.shutdown()
            Discovery.executor = ProcessPoolExecutor(max_workers=1)
            logging.info("Initialized new discovery pool")
            raise e

    @staticmethod
    def discover_stats() -> ClusterStats:
        deploy_count = -1
        sts_count = -1
        dms_count = -1
        rs_count = -1
        pod_count = -1
        node_count = -1
        job_count = -1
        try:
            deps: V1DeploymentList = client.AppsV1Api().list_deployment_for_all_namespaces(limit=1, _continue=None)
            remaining = deps.metadata.remaining_item_count or 0
            deploy_count = remaining + len(deps.items)
        except Exception:
            logging.error("Failed to count deployments", exc_info=True)

        try:
            sts: V1StatefulSetList = client.AppsV1Api().list_stateful_set_for_all_namespaces(limit=1, _continue=None)
            remaining = sts.metadata.remaining_item_count or 0
            sts_count = remaining + len(sts.items)
        except Exception:
            logging.error("Failed to count statefulsets", exc_info=True)

        try:
            dms: V1DaemonSetList = client.AppsV1Api().list_daemon_set_for_all_namespaces(limit=1, _continue=None)
            remaining = dms.metadata.remaining_item_count or 0
            dms_count = remaining + len(dms.items)
        except Exception:
            logging.error("Failed to count daemonsets", exc_info=True)

        try:
            rs: V1ReplicaSetList = client.AppsV1Api().list_replica_set_for_all_namespaces(limit=1, _continue=None)
            remaining = rs.metadata.remaining_item_count or 0
            rs_count = remaining + len(rs.items)
        except Exception:
            logging.error("Failed to count replicasets", exc_info=True)

        try:
            pods: V1PodList = client.CoreV1Api().list_pod_for_all_namespaces(limit=1, _continue=None)
            remaining = pods.metadata.remaining_item_count or 0
            pod_count = remaining + len(pods.items)
        except Exception:
            logging.error("Failed to count pods", exc_info=True)

        try:
            nodes: V1NodeList = client.CoreV1Api().list_node(limit=1, _continue=None)
            remaining = nodes.metadata.remaining_item_count or 0
            node_count = remaining + len(nodes.items)
        except Exception:
            logging.error("Failed to count nodes", exc_info=True)

        try:
            jobs: V1JobList = client.BatchV1Api().list_job_for_all_namespaces(limit=1, _continue=None)
            remaining = jobs.metadata.remaining_item_count or 0
            job_count = remaining + len(jobs.items)
        except Exception:
            logging.error("Failed to count jobs", exc_info=True)

        k8s_version: str = None
        try:
            k8s_version = client.VersionApi().get_code().git_version
        except Exception:
            logging.exception("Failed to get k8s server version")

        return ClusterStats(
            deployments=deploy_count,
            statefulsets=sts_count,
            daemonsets=dms_count,
            replicasets=rs_count,
            pods=pod_count,
            nodes=node_count,
            jobs=job_count,
            provider=cluster_provider.get_cluster_provider(),
            k8s_version=k8s_version,
        )


# This section below contains utility related to k8s python api objects (rather than hikaru)
def extract_containers(resource) -> List[V1Container]:
    """Extract containers from k8s python api object (not hikaru)"""
    try:
        containers = []
        if (
            isinstance(resource, V1Deployment)
            or isinstance(resource, V1DaemonSet)
            or isinstance(resource, V1StatefulSet)
            or isinstance(resource, V1Job)
        ):
            containers = resource.spec.template.spec.containers
        elif isinstance(resource, V1Pod):
            containers = resource.spec.containers

        return containers
    except Exception:  # may fail if one of the attributes is None
        logging.error(f"Failed to extract containers from {resource}", exc_info=True)
    return []


# This section below contains utility related to k8s python api objects (rather than hikaru)
def extract_containers_k8(resource) -> List[Container]:
    """Extract containers from k8s python api object (not hikaru)"""
    try:
        containers = []
        if (
            isinstance(resource, Deployment)
            or isinstance(resource, DaemonSet)
            or isinstance(resource, StatefulSet)
            or isinstance(resource, Job)
        ):
            containers = resource.spec.template.spec.containers
        elif isinstance(resource, Pod):
            containers = resource.spec.containers

        return containers
    except Exception:  # may fail if one of the attributes is None
        logging.error(f"Failed to extract containers from {resource}", exc_info=True)
    return []


def is_pod_ready(pod) -> bool:
    conditions = []
    if isinstance(pod, V1Pod):
        conditions = pod.status.conditions

    if isinstance(pod, Pod):
        conditions = pod.status.conditions

    for condition in conditions:
        if condition.type == "Ready":
            return condition.status.lower() == "true"

    return False


def should_report_pod(pod: Union[Pod, V1Pod]) -> bool:
    if is_pod_finished(pod):
        # we don't report completed/finished pods
        return False

    if isinstance(pod, V1Pod):
        owner_references = pod.metadata.owner_references
    else:
        owner_references = pod.metadata.ownerReferences
    if not owner_references:
        # Reporting unowned pods
        return True
    elif DISCOVERY_POD_OWNED_PODS:
        non_pod_owners = [reference for reference in owner_references if reference.kind.lower() != "pod"]
        # we report only if there are no owner references or they are pod owner refereces
        return len(non_pod_owners) == 0
    # we don't report pods with owner references
    return False


def is_pod_finished(pod) -> bool:
    try:
        if isinstance(pod, V1Pod) or isinstance(pod, Pod):
            # all containers in the pod have terminated, this pod should be removed by GC
            return pod.status.phase.lower() in ["succeeded", "failed"]
    except AttributeError:  # phase is an optional field
        return False


def extract_ready_pods(resource) -> int:
    try:
        if isinstance(resource, Deployment) or isinstance(resource, StatefulSet):
            return 0 if not resource.status.readyReplicas else resource.status.readyReplicas
        elif isinstance(resource, DaemonSet):
            return 0 if not resource.status.numberReady else resource.status.numberReady
        elif isinstance(resource, Pod):
            return 1 if is_pod_ready(resource) else 0
        elif isinstance(resource, V1Pod):
            return 1 if is_pod_ready(resource) else 0
        elif isinstance(resource, V1Deployment) or isinstance(resource, V1StatefulSet):
            return 0 if not resource.status.ready_replicas else resource.status.ready_replicas
        elif isinstance(resource, V1DaemonSet):
            return 0 if not resource.status.number_ready else resource.status.number_ready

        return 0
    except Exception:  # fields may not exist if all the pods are not ready - example: deployment crashpod
        logging.error(f"Failed to extract ready pods from {resource}", exc_info=True)
    return 0


def extract_total_pods(resource) -> int:
    try:
        if isinstance(resource, Deployment) or isinstance(resource, StatefulSet):
            # resource.spec.replicas can be 0, default value is 1
            return resource.spec.replicas if resource.spec.replicas is not None else 1
        elif isinstance(resource, DaemonSet):
            return 0 if not resource.status.desiredNumberScheduled else resource.status.desiredNumberScheduled
        elif isinstance(resource, Pod):
            return 1

        if isinstance(resource, V1Deployment) or isinstance(resource, V1StatefulSet):
            # resource.spec.replicas can be 0, default value is 1
            return resource.spec.replicas if resource.spec.replicas is not None else 1
        elif isinstance(resource, V1DaemonSet):
            return 0 if not resource.status.desired_number_scheduled else resource.status.desired_number_scheduled
        elif isinstance(resource, V1Pod):
            return 1
        return 0
    except Exception:
        logging.error(f"Failed to extract total pods from {resource}", exc_info=True)
    return 1


def is_release_managed_by_helm(labels: Optional[dict], annotations: Optional[dict]) -> bool:
    try:
        if labels:
            if labels.get("app.kubernetes.io/managed-by") == "Helm":
                return True

            helm_labels = set(key for key in labels.keys() if key.startswith("helm.") or key.startswith("meta.helm."))
            if helm_labels:
                return True

        if annotations:
            helm_annotations = set(
                key for key in annotations.keys() if key.startswith("helm.") or key.startswith("meta.helm.")
            )
            if helm_annotations:
                return True
    except Exception:
        logging.error(
            f"Failed to check if deployment was done via helm -> labels: {labels} | annotations: {annotations}"
        )

    return False


def extract_volumes(resource) -> List[V1Volume]:
    """Extract volumes from k8s python api object (not hikaru)"""
    try:
        volumes = []
        if (
            isinstance(resource, V1Deployment)
            or isinstance(resource, V1DaemonSet)
            or isinstance(resource, V1StatefulSet)
            or isinstance(resource, V1Job)
        ):
            volumes = resource.spec.template.spec.volumes
        elif isinstance(resource, V1Pod):
            volumes = resource.spec.volumes
        return volumes
    except Exception:  # may fail if one of the attributes is None
        logging.error(f"Failed to extract volumes from {resource}", exc_info=True)
    return []


def extract_volumes_k8(resource) -> List[Volume]:
    """Extract volumes from k8s python api object (not hikaru)"""
    try:
        volumes = []
        if (
            isinstance(resource, Deployment)
            or isinstance(resource, DaemonSet)
            or isinstance(resource, StatefulSet)
            or isinstance(resource, Job)
        ):
            volumes = resource.spec.template.spec.volumes
        elif isinstance(resource, Pod):
            volumes = resource.spec.volumes
        return volumes
    except Exception:  # may fail if one of the attributes is None
        logging.error(f"Failed to extract volumes from {resource}", exc_info=True)
    return []
