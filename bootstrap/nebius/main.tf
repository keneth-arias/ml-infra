variable "project_id" { type = string }
variable "subnet_id" { type = string }
variable "kubernetes_version" { type = string }

variable "public_api_allowed_cidrs" {
  description = "An empty list keeps the API private."
  type        = list(string)
  default     = []
  validation {
    condition = length(var.public_api_allowed_cidrs) <= 1 && alltrue([
      for cidr in var.public_api_allowed_cidrs : can(cidrnetmask(cidr)) && can(regex("^[0-9.]+/32$", cidr))
    ])
    error_message = "Allow at most the operator's single IPv4 /32 on the public endpoint."
  }
}

variable "existing_cluster_id" {
  description = "Use a separately managed cluster. Keep each cluster in its own backend key."
  type        = string
  default     = null
  validation {
    condition     = var.existing_cluster_id == null ? true : trimspace(var.existing_cluster_id) != ""
    error_message = "Supply an existing cluster ID or leave it unset."
  }
}

variable "worker_run" {
  description = "UTC allocation window shared by both workers. Reserve at least one of the 100 worker-hours for shutdown."
  type        = object({ id = string, start = string, cutoff = string, other_cost_usd = number })
  default     = null
  validation {
    condition = var.worker_run == null ? true : try(
      can(regex("^[a-z0-9][a-z0-9-]{0,39}$", var.worker_run.id)) &&
      endswith(var.worker_run.start, "Z") && endswith(var.worker_run.cutoff, "Z") &&
      timecmp(var.worker_run.cutoff, var.worker_run.start) > 0 &&
      var.worker_run.other_cost_usd >= 0 &&
      timecmp(var.worker_run.cutoff, timeadd(var.worker_run.start, "99h")) <= 0 &&
      timecmp(var.worker_run.cutoff, timeadd(var.worker_run.start,
        format("%ds", floor((250 - var.worker_run.other_cost_usd) / 1.5 * 3600) - 3600)
      )) <= 0,
      false
    )
    error_message = "Price all non-GPU charges and tax, allow one extra GPU-hour for release, and fit the UTC allocation window inside 99 hours and $250."
  }
}

variable "node_pools" {
  description = "Omit both pools for a worker-free foundation. Add each pool only when ready to allocate compute."
  type = object({
    cpu = optional(object({ platform = string, preset = string, count = number, disk_gib = number, disk_type = string }))
    gpu = optional(object({ platform = string, preset = string, count = number, disk_gib = number, disk_type = string }))
  })
  default  = {}
  nullable = false
  validation {
    condition     = var.worker_run != null || alltrue([for p in values(var.node_pools) : p == null])
    error_message = "Supply a priced worker_run window before enabling any CPU or GPU pool."
  }
  validation {
    condition     = alltrue([for p in values(var.node_pools) : p.count >= 1 && floor(p.count) == p.count && p.disk_gib > 0 && floor(p.disk_gib) == p.disk_gib if p != null])
    error_message = "Every enabled node pool needs a positive whole count and a positive disk size."
  }
  validation {
    condition     = var.node_pools.cpu == null ? true : startswith(var.node_pools.cpu.platform, "cpu-")
    error_message = "The CPU pool must use a CPU-only Nebius platform."
  }
  validation {
    condition = var.node_pools.gpu == null ? true : (
      var.node_pools.gpu.count == 1 && var.node_pools.gpu.platform == "gpu-rtx6000-a" &&
      var.node_pools.gpu.preset == "1gpu-24vcpu-218gb"
    )
    error_message = "The budgeted GPU pool supports exactly one single-GPU RTX PRO 6000 worker."
  }
  validation {
    condition     = var.existing_cluster_id == null || var.node_pools.cpu == null
    error_message = "CPU workers on an external cluster belong to its existing owner."
  }
}

moved {
  from = nebius_mk8s_v1_cluster.training
  to   = nebius_mk8s_v1_cluster.training[0]
}

resource "nebius_mk8s_v1_cluster" "training" {
  count     = var.existing_cluster_id == null ? 1 : 0
  parent_id = var.project_id
  name      = "ml-training-prod"
  control_plane = {
    subnet_id = var.subnet_id
    version   = var.kubernetes_version
    endpoints = {
      public_endpoint = length(var.public_api_allowed_cidrs) > 0 ? { allowed_cidrs = var.public_api_allowed_cidrs } : null
    }
  }
}

data "nebius_mk8s_v1_cluster" "connected" {
  count = var.existing_cluster_id == null ? 0 : 1
  id    = var.existing_cluster_id

  lifecycle {
    postcondition {
      condition = (
        self.parent_id == var.project_id &&
        self.control_plane.subnet_id == var.subnet_id &&
        self.control_plane.version == var.kubernetes_version
      )
      error_message = "The external cluster must match the project, subnet and Kubernetes version."
    }
  }
}

locals {
  window_open = var.worker_run == null ? false : (
    timecmp(plantimestamp(), var.worker_run.start) >= 0 && timecmp(plantimestamp(), var.worker_run.cutoff) < 0
  )
  cluster_id = var.existing_cluster_id != null ? data.nebius_mk8s_v1_cluster.connected[0].id : nebius_mk8s_v1_cluster.training[0].id
}

variable "existing_pricing_policy_id" {
  description = "Reuse a project-owned RTX PRO 6000 policy capped at $1.50/GPU-hour."
  type        = string
  default     = null
}

data "nebius_billing_v1_pricing_policy" "existing" {
  count = var.existing_pricing_policy_id == null ? 0 : 1
  id    = var.existing_pricing_policy_id
  lifecycle {
    postcondition {
      condition = (
        self.parent_id == var.project_id &&
        self.compute_instance_spec.v1.platform == "gpu-rtx6000-a" &&
        tonumber(self.pricing.max_price_v1.max_price) == 1.5
      )
      error_message = "The selected policy must match the project, RTX platform and $1.50 cap."
    }
  }
}

moved {
  from = nebius_billing_v1_pricing_policy.training
  to   = nebius_billing_v1_pricing_policy.training[0]
}

resource "nebius_billing_v1_pricing_policy" "training" {
  count                 = var.existing_pricing_policy_id == null ? 1 : 0
  parent_id             = var.project_id
  name                  = "airawarertx6000"
  compute_instance_spec = { v1 = { platform = "gpu-rtx6000-a" } }
  pricing               = { max_price_v1 = { max_price = "1.5" } }
}

locals {
  pricing_policy = var.existing_pricing_policy_id == null ? nebius_billing_v1_pricing_policy.training[0] : data.nebius_billing_v1_pricing_policy.existing[0]
}

variable "artifact_bucket_name" {
  description = "Optional private, versioned bucket for run artifacts, separate from the state backend."
  type        = string
  default     = null
}

resource "nebius_storage_v1_bucket" "artifacts" {
  count                 = var.artifact_bucket_name == null ? 0 : 1
  parent_id             = var.project_id
  name                  = var.artifact_bucket_name
  versioning_policy     = "ENABLED"
  default_storage_class = "ENHANCED_THROUGHPUT"
  force_storage_class   = true
  max_size_bytes        = 64 * 1024 * 1024 * 1024
}

resource "nebius_mk8s_v1_node_group" "training" {
  for_each  = { for name, pool in var.node_pools : name => pool if pool != null && local.window_open }
  parent_id = local.cluster_id
  name      = "training-${each.key}"
  labels = {
    airaware-run    = var.worker_run.id
    airaware-cutoff = formatdate("YYYYMMDDhhmmss", var.worker_run.cutoff)
  }
  fixed_node_count = each.value.count
  version          = var.kubernetes_version
  lifecycle {
    precondition {
      condition     = timecmp(timestamp(), var.worker_run.cutoff) < 0
      error_message = "The allocation cutoff has passed. Discard the worker launch plan."
    }
    precondition {
      condition     = each.key != "gpu" || local.pricing_policy.status.scheduling_state == "SCHEDULING_STATE_ALLOWED"
      error_message = "The RTX PRO 6000 spot price policy must allow scheduling before the GPU pool can launch."
    }
  }
  strategy = {
    max_surge       = { count = 0 }
    max_unavailable = { count = 1 }
    drain_timeout   = "5m"
  }
  template = {
    os                  = "ubuntu24.04"
    gpu_settings        = each.key == "gpu" ? { drivers_preset = "cuda13.0" } : null
    preemptible         = each.key == "gpu" ? {} : null
    spot_pricing_policy = each.key == "gpu" ? { id = local.pricing_policy.id } : null
    metadata = { labels = merge(
      { "airaware.training/pool" = "training-${each.key}" },
      each.key == "gpu" ? { "skypilot.co/accelerator" = "rtxpro6000" } : {}
    ) }
    taints             = each.key == "gpu" ? [{ key = "nvidia.com/gpu", value = "true", effect = "NO_SCHEDULE" }] : []
    network_interfaces = [{ subnet_id = var.subnet_id }]
    resources          = { platform = each.value.platform, preset = each.value.preset }
    boot_disk          = { type = each.value.disk_type, size_gibibytes = each.value.disk_gib }
  }
}

output "cluster_id" {
  value = local.cluster_id
}

output "pricing_policy_id" {
  value = local.pricing_policy.id
}

output "artifact_bucket_name" {
  value = one(nebius_storage_v1_bucket.artifacts[*].name)
}
