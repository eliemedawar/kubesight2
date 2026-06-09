export const STORAGE_TOOLTIPS = {
  pv: "A PersistentVolume (PV) is cluster storage provisioned by an administrator or dynamically via a StorageClass.",
  pvc: "A PersistentVolumeClaim (PVC) is a request for storage by a pod. It binds to a matching PersistentVolume.",
  storageClass: "A StorageClass defines how PersistentVolumes are dynamically provisioned in the cluster.",
  readWriteOnce: "ReadWriteOnce (RWO) allows the volume to be mounted as read-write by a single node.",
  readWriteMany: "ReadWriteMany (RWX) allows the volume to be mounted as read-write by many nodes simultaneously.",
};

export function isCreatingPvc(state) {
  if (!state) return false;
  if (state.workloadType === "PersistentVolumeClaim") return true;
  return state.storage?.pvcMode === "new";
}

export function isManualPvMode(state) {
  return Boolean(state?.storage?.advanced?.createManualPv);
}

export function getEffectiveStorageClass(state) {
  if (isManualPvMode(state)) {
    return "";
  }
  return state?.storage?.newPvc?.storageClass || "";
}

export function validateStorageConfig(state, storageClasses = []) {
  const errors = [];
  const warnings = [];

  if (!isCreatingPvc(state)) {
    return { errors, warnings };
  }

  const storage = state.storage || {};
  const newPvc = storage.newPvc || {};
  const manualPv = isManualPvMode(state);
  const selectedClass = getEffectiveStorageClass(state);
  const hasClasses = storageClasses.length > 0;

  if (!manualPv) {
    if (hasClasses && !selectedClass) {
      errors.push("Select a StorageClass for the new PVC.");
    }

    if (!hasClasses) {
      warnings.push(
        "No StorageClass is available in this cluster. This PVC may remain Pending unless a matching PersistentVolume exists.",
      );
    }
  }

  if (newPvc.accessMode === "ReadWriteMany") {
    warnings.push(
      "ReadWriteMany is not supported by many local Kubernetes installations. Verify that your storage provider supports RWX volumes.",
    );
  }

  return { errors, warnings };
}

export function getStorageReadiness(state, storageClasses = []) {
  if (!isCreatingPvc(state)) {
    return null;
  }

  const storage = state.storage || {};
  const advanced = storage.advanced || {};
  const manualPv = Boolean(advanced.createManualPv);
  const selectedClass = getEffectiveStorageClass(state);
  const hasClasses = storageClasses.length > 0;
  const pvName = (advanced.pvName || "").trim() || `${storage.newPvc?.name || "data"}-pv`;

  if (manualPv) {
    return {
      level: "yellow",
      message: `PVC will bind to manually created PersistentVolume: ${pvName}`,
    };
  }

  if (selectedClass) {
    return {
      level: "green",
      message: `PVC will be dynamically provisioned using StorageClass: ${selectedClass}`,
    };
  }

  if (!hasClasses) {
    return {
      level: "red",
      message:
        "No StorageClass found. PVC is likely to remain Pending until a matching PersistentVolume is created.",
    };
  }

  return {
    level: "yellow",
    message: "PVC will rely on an existing PersistentVolume matching this claim.",
  };
}
