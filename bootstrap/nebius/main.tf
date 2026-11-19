variable "project_id" { type = string }
variable "subnet_id" { type = string }
variable "kubernetes_version" { type = string }

variable "public_api_allowed_cidrs" {
  description = "An empty list keeps the API private."
  type        = list(string)
  default     = []
}

variable "node_pools" {
  description = "Select region-specific CPU and GPU platforms and presets after reviewing capacity and cost."
  type = object({
    cpu = object({ platform = string, preset = string, count = number, disk_gib = number, disk_type = string })
    gpu = object({ platform = string, preset = string, count = number, disk_gib = number, disk_type = string })
  })
  validation {
    condition     = alltrue([for p in values(var.node_pools) : p.count >= 1 && floor(p.count) == p.count && p.disk_gib > 0])
    error_message = "Each node pool requires a positive whole node count and disk size."
  }
}

resource "nebius_mk8s_v1_cluster" "training" {
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

resource "nebius_mk8s_v1_node_group" "training" {
  for_each         = var.node_pools
  parent_id        = nebius_mk8s_v1_cluster.training.id
  name             = "training-${each.key}"
  fixed_node_count = each.value.count
  version          = var.kubernetes_version
  template = {
    taints             = each.key == "gpu" ? [{ key = "nvidia.com/gpu", value = "true", effect = "NO_SCHEDULE" }] : []
    network_interfaces = [{ subnet_id = var.subnet_id }]
    resources          = { platform = each.value.platform, preset = each.value.preset }
    boot_disk          = { type = each.value.disk_type, size_gibibytes = each.value.disk_gib }
  }
}

output "cluster_id" {
  value = nebius_mk8s_v1_cluster.training.id
}
