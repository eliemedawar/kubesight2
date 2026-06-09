/**
 * Determines which dashboard widgets are visible for the current user.
 * UX-only — backend authorization remains authoritative.
 */

function hasAllPermissions(auth, permissions = []) {
  if (!permissions.length) {
    return true;
  }
  return permissions.every((key) => auth.hasPermission(key));
}

function hasAnyPermissions(auth, permissions = []) {
  if (!permissions.length) {
    return true;
  }
  return auth.hasAnyPermission(permissions);
}

export function isWidgetVisible(widget, auth, { clusterId } = {}) {
  if (!widget) {
    return false;
  }

  if (widget.alwaysVisible) {
    return true;
  }

  if (!auth?.user) {
    return false;
  }

  if (widget.requiresClusterAccess && clusterId && !auth.canAccessCluster(clusterId)) {
    return false;
  }

  if (widget.requiresNamespaceAccess && !auth.hasAnyNamespaceAccess()) {
    return false;
  }

  if (widget.requiredPermissions?.length && !hasAllPermissions(auth, widget.requiredPermissions)) {
    return false;
  }

  if (widget.requiredAnyPermissions?.length && !hasAnyPermissions(auth, widget.requiredAnyPermissions)) {
    return false;
  }

  if (widget.hideIfPermissions?.length) {
    const blocked = widget.hideIfPermissions.some((key) => auth.hasPermission(key));
    if (blocked) {
      return false;
    }
  }

  return true;
}

export function getVisibleWidgets(registry, auth, context = {}) {
  return registry.filter((widget) => isWidgetVisible(widget, auth, context));
}

export function groupWidgetsBySection(widgets) {
  return widgets.reduce((groups, widget) => {
    const section = widget.section || "details";
    if (!groups[section]) {
      groups[section] = [];
    }
    groups[section].push(widget);
    return groups;
  }, {});
}
