mock_provider "nebius" {
  mock_resource "nebius_mk8s_v1_cluster" {
    defaults = { id = "mk8scluster-e07testonly000000000001" }
  }
  mock_data "nebius_billing_v1_pricing_policy" {
    defaults = {
      parent_id             = "project-e07testonly000000000001"
      compute_instance_spec = { v1 = { platform = "gpu-rtx6000-a" } }
      pricing               = { max_price_v1 = { max_price = "1.5" } }
      status                = { scheduling_state = "SCHEDULING_STATE_ALLOWED" }
    }
  }
  mock_data "nebius_mk8s_v1_cluster" {
    defaults = {
      parent_id     = "project-e07testonly000000000001"
      control_plane = { subnet_id = "vpcsubnet-e07testonly000000000001", version = "1.36" }
    }
  }
}

variables {
  project_id         = "project-e07testonly000000000001"
  subnet_id          = "vpcsubnet-e07testonly000000000001"
  kubernetes_version = "1.36"
  worker_run         = { other_cost_usd = 100, id = "test-run", start = timeadd(timestamp(), "-1h"), cutoff = timeadd(timestamp(), "2h") }
}

run "private_foundation_has_no_workers" {
  command = plan
  assert {
    condition = (
      length(nebius_mk8s_v1_cluster.training) == 1 &&
      length(nebius_mk8s_v1_node_group.training) == 0 &&
      nebius_mk8s_v1_cluster.training[0].control_plane.endpoints.public_endpoint == null &&
      nebius_billing_v1_pricing_policy.training[0].pricing.max_price_v1.max_price == "1.5"
    )
    error_message = "The default must plan only the private control plane and capped policy."
  }
}

run "single_egress_ip_without_workers" {
  command = plan
  variables { public_api_allowed_cidrs = ["203.0.113.42/32"] }
  assert {
    condition = (
      toset(nebius_mk8s_v1_cluster.training[0].control_plane.endpoints.public_endpoint.allowed_cidrs) == toset(["203.0.113.42/32"]) &&
      length(nebius_mk8s_v1_node_group.training) == 0
    )
    error_message = "Only the approved host may reach the public endpoint; worker allocation is independent."
  }
}

run "reject_public_subnet_access" {
  command = plan
  variables { public_api_allowed_cidrs = ["0.0.0.0/0"] }
  expect_failures = [var.public_api_allowed_cidrs]
}

run "reject_additional_source_host" {
  command = plan
  variables { public_api_allowed_cidrs = ["203.0.113.42/32", "203.0.113.43/32"] }
  expect_failures = [var.public_api_allowed_cidrs]
}

run "use_connected_cluster" {
  command = plan
  variables { existing_cluster_id = "mk8scluster-e07testonly000000000001" }
  assert {
    condition = (
      length(nebius_mk8s_v1_cluster.training) == 0 &&
      length(nebius_mk8s_v1_node_group.training) == 0 &&
      output.cluster_id == "mk8scluster-e07testonly000000000001"
    )
    error_message = "A connected cluster must retain its existing control plane and workers."
  }
}

run "cpu_only" {
  command = plan
  variables { node_pools = { cpu = { platform = "cpu-d3", preset = "4vcpu-16gb", count = 1, disk_gib = 128, disk_type = "NETWORK_SSD" } } }
  assert {
    condition     = keys(nebius_mk8s_v1_node_group.training) == ["cpu"] && nebius_mk8s_v1_node_group.training["cpu"].template.preemptible == null
    error_message = "Enabling the CPU pool must keep the GPU pool absent."
  }
}

run "gpu_only_connected" {
  command = plan
  variables {
    existing_cluster_id = "mk8scluster-e07testonly000000000001"
    node_pools          = { gpu = { platform = "gpu-rtx6000-a", preset = "1gpu-24vcpu-218gb", count = 1, disk_gib = 128, disk_type = "NETWORK_SSD" } }
  }
  assert {
    condition = (
      keys(nebius_mk8s_v1_node_group.training) == ["gpu"] &&
      nebius_mk8s_v1_node_group.training["gpu"].parent_id == var.existing_cluster_id &&
      nebius_mk8s_v1_node_group.training["gpu"].fixed_node_count == 1 &&
      nebius_mk8s_v1_node_group.training["gpu"].template.preemptible != null &&
      nebius_mk8s_v1_node_group.training["gpu"].strategy.max_surge.count == 0 &&
      nebius_mk8s_v1_node_group.training["gpu"].strategy.drain_timeout == "5m"
    )
    error_message = "Plan one preemptible GPU in the external cluster, with bounded replacement."
  }
}


run "reject_shared_cpu" {
  command = plan
  variables {
    existing_cluster_id = "mk8scluster-e07testonly000000000001"
    node_pools          = { cpu = { platform = "cpu-d3", preset = "4vcpu-16gb", count = 1, disk_gib = 128, disk_type = "NETWORK_SSD" } }
  }
  expect_failures = [var.node_pools]
}

run "reject_second_gpu" {
  command = plan
  variables {
    node_pools = { gpu = { platform = "gpu-rtx6000-a", preset = "8gpu-192vcpu-1744gb", count = 1, disk_gib = 128, disk_type = "NETWORK_SSD" } }
  }
  expect_failures = [var.node_pools]
}


run "versioned_artifact_storage" {
  command = plan
  variables { artifact_bucket_name = "airaware-test-artifacts" }
  assert {
    condition = (
      length(nebius_mk8s_v1_node_group.training) == 0 &&
      nebius_storage_v1_bucket.artifacts[0].versioning_policy == "ENABLED" &&
      nebius_storage_v1_bucket.artifacts[0].max_size_bytes == 68719476736
    )
    error_message = "Artifact storage must be bounded, versioned and independent of workers."
  }
}


run "reuse_policy" {
  command = plan
  variables {
    existing_pricing_policy_id = "pricingpolicy-e07testonly000000000001"
  }
  assert {
    condition     = length(nebius_billing_v1_pricing_policy.training) == 0 && output.pricing_policy_id == var.existing_pricing_policy_id
    error_message = "Reusing a policy must not create a second policy."
  }
}

run "blocked_policy" {
  command = plan
  variables {
    existing_pricing_policy_id = "pricingpolicy-e07testonly000000000001"
    node_pools                 = { gpu = { platform = "gpu-rtx6000-a", preset = "1gpu-24vcpu-218gb", count = 1, disk_gib = 128, disk_type = "NETWORK_SSD" } }
  }
  override_data {
    target = data.nebius_billing_v1_pricing_policy.existing[0]
    values = {
      parent_id             = "project-e07testonly000000000001"
      compute_instance_spec = { v1 = { platform = "gpu-rtx6000-a" } }
      pricing               = { max_price_v1 = { max_price = "1.5" } }
      status                = { scheduling_state = "SCHEDULING_STATE_BLOCKED" }
    }
  }
  expect_failures = [nebius_mk8s_v1_node_group.training["gpu"]]
}


run "reject_gpu_in_cpu_pool" {
  command = plan
  variables {
    node_pools = { cpu = { platform = "gpu-rtx6000-a", preset = "1gpu-24vcpu-218gb", count = 1, disk_gib = 128, disk_type = "NETWORK_SSD" } }
  }
  expect_failures = [var.node_pools]
}


run "reject_second_cpu" {
  command = plan
  variables {
    node_pools = { cpu = { platform = "cpu-d3", preset = "4vcpu-16gb", count = 2, disk_gib = 128, disk_type = "NETWORK_SSD" } }
  }
  expect_failures = [var.node_pools]
}

run "reject_blank_existing_pricing_policy" {
  command = plan
  variables { existing_pricing_policy_id = " " }
  expect_failures = [var.existing_pricing_policy_id]
}

run "gpu_on_owned_foundation" {
  command = apply
  variables {
    node_pools = { gpu = { platform = "gpu-rtx6000-a", preset = "1gpu-24vcpu-218gb", count = 1, disk_gib = 128, disk_type = "NETWORK_SSD" } }
  }
  override_resource {
    target = nebius_billing_v1_pricing_policy.training[0]
    values = {
      id     = "pricingpolicy-e07testonly000000000001"
      status = { scheduling_state = "SCHEDULING_STATE_ALLOWED" }
    }
  }
  assert {
    condition = (
      nebius_mk8s_v1_node_group.training["gpu"].parent_id == nebius_mk8s_v1_cluster.training[0].id &&
      nebius_mk8s_v1_node_group.training["gpu"].template.spot_pricing_policy.id == nebius_billing_v1_pricing_policy.training[0].id &&
      nebius_mk8s_v1_node_group.training["gpu"].fixed_node_count == 1
    )
    error_message = "The owned cluster must attach one GPU to its approved managed policy."
  }
}
