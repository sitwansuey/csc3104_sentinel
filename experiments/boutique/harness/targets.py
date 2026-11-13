"""Recovery target profiles from the scenario catalog (experiments/boutique/catalog.json).

Every per-target constant the live harness needs lives in the catalog and is
validated by scenarios.validate(): container, ServiceAccount, image repository
and release, gRPC port and probe, the reviewed Deployment spec overrides, the
storefront impact a fault must show, the Guard's resource ceilings, the primary
fault and its fallback, and the scopes where the target may be repaired.

The executor Role is generated here from deploy/sentinel/executor-rbac.yaml. It
names exactly the targets enabled in one lab scope (resourceNames), never the
whole namespace. A pair-only target (redis-cart, P4.05) is never enabled in a
scope: only a scenario-pair run names it, together with its partner.
"""
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path

from experiments.boutique.harness import scenarios
from experiments.boutique.harness.scenarios import CatalogError

ROOT = Path(__file__).resolve().parents[3]
EXECUTOR_RBAC = ROOT / "deploy/sentinel/executor-rbac.yaml"
_RBAC_NAMESPACE = "sentinel-demo"
_RBAC_NAMES = 'resourceNames: ["demo-api"]'
_RBAC_VERBS = '    verbs: ["get", "patch"]\n'
# P5.07: a SCALE run's Executor lists the HorizontalPodAutoscalers in its namespace, read only, so the Guard can
# escalate a SCALE that an autoscaler would fight. Other runs' Roles do not get it.
AUTOSCALER_READ_RULE = ('  - apiGroups: ["autoscaling"]\n    resources: ["horizontalpodautoscalers"]\n'
                        '    verbs: ["list"]\n')


@dataclass(frozen=True)
class TargetProfile:
    """One validated catalog target. Mapping fields are copies; changing them changes nothing."""

    name: str
    label: str
    container: str
    service_account: str
    image_repository: str
    release: str
    port: int
    scopes: tuple
    primary_scenario: str | None
    fallback_scenario: str | None
    isolated_lab: bool
    spec_overrides: dict
    probe: dict
    fault_storefront_impact: dict
    ceilings: dict
    pair_only: bool = False
    # The reviewed minimum replica count (P5.07); None for a target with no scale fault.
    min_replicas: int | None = None

    @property
    def registry(self):
        return self.image_repository.split("/", 1)[0]

    @property
    def image(self):
        """The upstream release image, by tag; the harness resolves and pins its digest."""
        return self.image_repository + ":" + self.release

    @property
    def address(self):
        """The in-cluster gRPC address of the target's Service."""
        return f"{self.name}:{self.port}"

    @property
    def operator_scenarios(self):
        """Scenarios the operator-mode sequence may inject: the primary fault and its fallback."""
        return tuple(item for item in (self.primary_scenario, self.fallback_scenario) if item)


def default_target(catalog=None):
    return (catalog or scenarios.load())["default_target"]


def target_names(catalog=None):
    """The targets a single-target run may select; pair-only targets run only inside a pair."""
    return tuple(sorted(name for name, target in (catalog or scenarios.load())["targets"].items()
                        if not target.get("pair_only")))


def profile(name=None, catalog=None):
    """The TargetProfile for name (the catalog default when None); CatalogError for an unknown target."""
    catalog = catalog or scenarios.load()
    name = catalog["default_target"] if name is None else name
    if not isinstance(name, str) or name not in catalog["targets"]:
        raise CatalogError("Unknown recovery target: " + str(name))
    target = deepcopy(catalog["targets"][name])
    target.pop("kind")
    target["scopes"] = tuple(target["scopes"])
    return TargetProfile(name=name, **target)


def scope_name(cluster_id, context, namespace, catalog=None):
    """The catalog scope with exactly this (cluster_id, context, namespace); CatalogError otherwise."""
    catalog = catalog or scenarios.load()
    for name, scope in catalog["scopes"].items():
        if (scope["cluster_id"], scope["context"], scope["namespace"]) == (cluster_id, context, namespace):
            return name
    raise CatalogError("No catalog scope for context " + str(context) + " and namespace " + str(namespace))


def enabled_targets(scope, catalog=None):
    """Sorted names of the targets offered in one catalog scope; pair-only targets are never offered."""
    catalog = catalog or scenarios.load()
    if scope not in catalog["scopes"]:
        raise CatalogError("Unknown catalog scope: " + str(scope))
    return tuple(sorted(name for name, target in catalog["targets"].items()
                        if scope in target["scopes"] and not target.get("pair_only")))


def apply_spec_overrides(spec, target):
    """A copy of a Deployment spec with the target's reviewed overrides (for example strategy Recreate)."""
    result = deepcopy(spec)
    result.update(deepcopy(target.spec_overrides))
    return result


def executor_rbac(namespace, names, template=None, *, autoscaler_read=False):
    """The executor ServiceAccount, Role and RoleBinding for namespace, limited to exactly names.

    The Role grants get and patch on Deployments by resourceNames only. The
    template must contain its one placeholder resourceNames line, so a changed
    template cannot silently widen or drop the scope. autoscaler_read (a SCALE
    run only) adds list on horizontalpodautoscalers in the namespace, read only.
    """
    names = sorted(set(names))
    if not names or not all(isinstance(name, str) and scenarios._NAME.fullmatch(name) for name in names):
        raise CatalogError("The executor Role needs at least one Deployment name")
    if not isinstance(namespace, str) or not scenarios._NAME.fullmatch(namespace):
        raise CatalogError("Invalid executor namespace")
    text = EXECUTOR_RBAC.read_text(encoding="utf-8") if template is None else template
    if text.count(_RBAC_NAMES) != 1 or text.count("resourceNames") != 1:
        raise CatalogError("The executor RBAC template must name its Deployments in exactly one resourceNames line")
    if autoscaler_read:
        if text.count(_RBAC_VERBS) != 1:
            raise CatalogError("The executor RBAC template must have exactly one Deployment verbs line")
        text = text.replace(_RBAC_VERBS, _RBAC_VERBS + AUTOSCALER_READ_RULE)
    return (text.replace(_RBAC_NAMES, "resourceNames: " + json.dumps(names))
                .replace(_RBAC_NAMESPACE, namespace))


def impact_matches(target, journey):
    """Whether a measured storefront journey shows exactly the catalog's predicted fault impact."""
    successful = {"home": journey.get("home_status") == 200,
                  "add_to_cart": journey.get("add_to_cart_status") in {302, 303},
                  "cart": journey.get("cart_status") == 200,
                  "checkout": journey.get("checkout_status") == 200}
    return all(successful[part] == (expected == "ok") for part, expected in target.fault_storefront_impact.items())


def impact_phrase(target):
    """Plain words for the failing storefront steps, for example 'cart and checkout fail'."""
    names = {"home": "home", "add_to_cart": "add to cart", "cart": "cart", "checkout": "checkout"}
    failing = [names[part] for part in scenarios.IMPACT_PARTS if target.fault_storefront_impact[part] == "fail"]
    joined = failing[0] if len(failing) == 1 else ", ".join(failing[:-1]) + " and " + failing[-1]
    return joined + " fail" + ("s" if len(failing) == 1 else "")
