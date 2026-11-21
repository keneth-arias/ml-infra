mock_provider "nebius" {
  mock_resource "nebius_mk8s_v1_cluster" {
    defaults = { id = "mk8scluster-e07testonly000000000001" }
  }
}

variables {
  project_id         = "project-e07testonly000000000001"
  subnet_id          = "vpcsubnet-e07testonly000000000001"
  kubernetes_version = "1.36"
  node_pools         = { cpu = { platform = "cpu-d3", preset = "4vcpu-16gb", count = 1, disk_gib = 128, disk_type = "NETWORK_SSD" } }
}

run "workers_require_window" {
  command         = plan
  expect_failures = [var.node_pools]
}

run "expired_run_has_no_workers" {
  command = plan
  variables {
    worker_run = { other_cost_usd = 100, id = "old-run", start = "2020-01-01T00:00:00Z", cutoff = "2020-01-02T00:00:00Z" }
  }
  assert {
    condition     = length(nebius_mk8s_v1_node_group.training) == 0
    error_message = "Replanning an expired run must never resurrect a worker."
  }
}

run "reject_over_budget_window" {
  command = plan
  variables {
    worker_run = { other_cost_usd = 100, id = "oversized", start = "2030-01-01T00:00:00Z", cutoff = "2030-01-10T00:00:00Z" }
  }
  expect_failures = [var.worker_run]
}

run "reject_unfunded_retention" {
  command = plan
  variables {
    worker_run = { id = "retention", start = "2030-01-01T00:00:00Z", cutoff = "2030-01-01T01:00:00Z", other_cost_usd = 100.01 }
  }
  expect_failures = [var.worker_run]
}

# Keep one mock worker in state between the two runs to exercise the expiry transition.
run "allocate_mock_worker" {
  command = apply
  variables {
    worker_run = {
      id             = "expiry-drill"
      start          = timeadd(timestamp(), "-1h")
      cutoff         = format("%sT18:45:29Z", substr(timeadd(timestamp(), "24h"), 0, 10))
      other_cost_usd = 100
    }
  }
  assert {
    condition     = nebius_mk8s_v1_node_group.training["cpu"].labels["airaware-run"] == "expiry-drill"
    error_message = "Cleanup needs a cloud-level ownership label on the worker group."
  }
  assert {
    condition     = nebius_mk8s_v1_node_group.training["cpu"].labels["airaware-cutoff"] == replace(var.worker_run.cutoff, "/[-:TZ]/", "")
    error_message = "The expiry guard expects the exact UTC date and 24-hour time, including afternoon hours."
  }
  assert {
    condition = (
      length(nebius_mk8s_v1_node_group.training) == 1 &&
      nebius_mk8s_v1_node_group.training["cpu"].strategy.max_surge.count == 0 &&
      nebius_mk8s_v1_node_group.training["cpu"].strategy.max_unavailable.count == 1 &&
      nebius_mk8s_v1_node_group.training["cpu"].strategy.drain_timeout == "5m" &&
      nebius_mk8s_v1_node_group.training["cpu"].template.metadata.labels["airaware.training/pool"] == "training-cpu" &&
      nebius_mk8s_v1_node_group.training["cpu"].template.os == "ubuntu24.04" &&
      nebius_mk8s_v1_node_group.training["cpu"].template.preemptible == null
    )
    error_message = "The CPU pilot requires one labeled regular pool, zero surge and a bounded drain."
  }
}

run "release_mock_worker" {
  command = apply
  variables {
    worker_run = { id = "expiry-drill", start = "2020-01-01T00:00:00Z", cutoff = "2020-01-02T00:00:00Z", other_cost_usd = 100 }
  }
  assert {
    condition     = length(nebius_mk8s_v1_node_group.training) == 0 && length(nebius_mk8s_v1_cluster.training) == 1
    error_message = "Expiry must release the run worker and preserve its control plane."
  }
}
