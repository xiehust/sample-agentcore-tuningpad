"""Task-only CPU admission check; never training, CUDA, AWS or an existing Ray cluster.

backend/.venv/bin/python SCRIPT generate /tmp/UNIQUE-ARTIFACT-DIR
PRIVATE_TOOLKIT/.venv/bin/python SCRIPT check /tmp/UNIQUE-ARTIFACT-DIR

Real Hydra compose and TQ 0.1.8 bootstrap; synthetic training PG / actor bodies.
The named synthetic GPU marker is a custom resource, NOT Ray's GPU resource.
Each case owns ray.init(address='local') / shutdown and a private temporary dir.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
KEY = "transfer_queue.backend.SimpleStorage.num_data_storage_units"
MARKER = "TP_SYNTHETIC_GPU_NOT_HARDWARE"


def generate(work):
    sys.path.insert(0, str(ROOT / "backend"))
    from app.catalog.instances import CATALOG
    from app.planner import plan
    from app.render import train

    work.mkdir(exist_ok=False)
    rows = []
    for mode in ("sync", "colocate_async"):
        for profile in ("fsdp", "megatron"):
            for tuning in ("full", "lora"):
                for itype, nodes in (
                    ("p5.4xlarge", 1), ("p5.4xlarge", 2),
                    ("p5.48xlarge", 1), ("p5.48xlarge", 2),
                ):
                    spec = CATALOG[itype]
                    p = plan(params_b=2.27, spec=spec, nodes=nodes,
                             prefer=profile, tuning=tuning).to_dict()
                    hydra = train.overrides(
                        plan=p, params=train.merge_params({}), spec=spec, nodes=nodes,
                        model_path="/synthetic/model", train_file="/synthetic/train.parquet",
                        val_file=None, ckpt_dir="/synthetic/ckpt", project="cpu-admission",
                        experiment="not-training", training_mode=mode,
                    )
                    job = train.rayjob(
                        run_id="synthetic", name="synthetic", namespace="synthetic",
                        image="synthetic", spec=spec, nodes=nodes, group="synthetic",
                        env=[], config_map="synthetic", service_account="synthetic", pvc="synthetic",
                    )
                    rows.append(dict(mode=mode, profile=profile, tuning=tuning, itype=itype,
                                     nodes=nodes, hydra=hydra, rayjob=job))
    (work / "renders.json").write_text(json.dumps(rows, indent=2))
    print(f"Generated {len(rows)} renders in {work}", flush=True)


def compose(row):
    from hydra import compose as hydra_compose, initialize_config_module

    name = "ppo_megatron_trainer" if row["profile"] == "megatron" else "ppo_trainer"
    with initialize_config_module(config_module="verl.trainer.config", version_base=None):
        return hydra_compose(config_name=name, overrides=row["hydra"])


def check(work):
    versions = {p: version(p) for p in ("ray", "verl", "transferqueue", "torch", "hydra-core")}
    assert versions["ray"] == "2.56.0"
    assert versions["verl"] == "0.9.0" and versions["transferqueue"] == "0.1.8"
    rows = json.loads((work / "renders.json").read_text())
    results = []
    for row in rows:
        cfg = compose(row)
        small = row["itype"] == "p5.4xlarge" and row["nodes"] == 1
        assert cfg.transfer_queue.backend.SimpleStorage.num_data_storage_units == (2 if small else 8)
        assert cfg.transfer_queue.backend.SimpleStorage.total_storage_size == 100000
        assert cfg.transfer_queue.backend.storage_backend == "SimpleStorage"
        assert cfg.actor_rollout_ref.rollout.agent.num_workers == 1
        assert cfg.reward.num_workers == 8 and cfg.reward.reward_manager.name == "naive"
        assert not cfg.reward.reward_model.enable and not cfg.distillation.enabled
        assert cfg.trainer.v1.trainer_mode == f"agentcore_{row['mode']}"
        assert cfg.trainer.nnodes == row["nodes"]
        assert cfg.trainer.n_gpus_per_node == (1 if row["itype"] == "p5.4xlarge" else 8)
        results.append({k: row[k] for k in ("mode", "profile", "tuning", "itype", "nodes")})
    (work / "compose-results.json").write_text(json.dumps(dict(versions=versions, passed=results), indent=2))
    print(f"PASS {len(results)} exact Hydra compositions; versions={versions}", flush=True)
    for case in ("original", "sync", "colocate_async"):
        with (work / f"{case}.log").open("w") as log:
            subprocess.run([sys.executable, str(Path(__file__).resolve()), "case", str(work), case],
                           stdout=log, stderr=subprocess.STDOUT, check=True, timeout=210)
        print(f"PASS local Ray case={case}; log={work / (case + '.log')}", flush=True)


def run_case(work, case):
    # Do not inherit a global cluster or advertise real GPUs; CPU oversubscription
    # of the host is irrelevant here: this tests the exact 8-logical-CPU admission.
    os.environ.pop("RAY_ADDRESS", None)
    os.environ.update(RAY_USAGE_STATS_ENABLED="0", HF_HUB_OFFLINE="1",
                      CUDA_VISIBLE_DEVICES="", OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    import psutil
    import ray
    from omegaconf import OmegaConf
    from ray.util import placement_group, placement_group_table, remove_placement_group
    from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy, PlacementGroupSchedulingStrategy
    from verl.single_controller.ray.base import ResourcePoolManager
    from verl.trainer.main_ppo import TaskRunnerV1

    def deadline(signum, frame):
        raise TimeoutError("CPU admission case exceeded 180 seconds")

    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(180)
    rows = json.loads((work / "renders.json").read_text())
    row = next(r for r in rows if r["mode"] == ("sync" if case == "original" else case)
               and r["itype"] == "p5.4xlarge" and r["nodes"] == 1
               and r["profile"] == "fsdp" and r["tuning"] == "full")
    if case == "original":
        row["hydra"] = [h for h in row["hydra"] if not h.startswith(KEY + "=")]
    cfg = compose(row)
    cfg.transfer_queue.enable = True  # exact V1 entrypoint behavior, not an override
    units = cfg.transfer_queue.backend.SimpleStorage.num_data_storage_units
    cpus_per_gpu = ResourcePoolManager({}, {}).max_colocate_count
    assert cpus_per_gpu == 3 and units == (8 if case == "original" else 2)
    report = dict(case=case, storage_units=units, cpu_capacity=8,
                  training_pg_cpu=cpus_per_gpu, synthetic_gpu_marker=MARKER,
                  real_gpu_or_training=False, result="FAIL")

    @ray.remote
    class Bootstrap:
        def start(self, conf):
            import transfer_queue as tq
            from omegaconf import OmegaConf

            final = tq.init(OmegaConf.create(conf))
            controller = ray.get_actor("TransferQueueController", namespace="transfer_queue")
            storage = [ray.get_actor(f"TransferQueueStorageUnit#{i}")
                       for i in range(final.backend.SimpleStorage.num_data_storage_units)]
            return dict(controller=ray.get(controller.__ray_call__.remote(
                            lambda _: ray.get_runtime_context().get_assigned_resources())),
                        storage=ray.get([a.__ray_call__.remote(
                            lambda _: ray.get_runtime_context().get_assigned_resources()) for a in storage]))

        def close(self):
            import transfer_queue as tq
            tq.close()

    @ray.remote
    class DefaultActorShape:
        """Only the sourced admission shape; NOT model/reward/agent execution."""
        def ping(self):
            return ray.get_runtime_context().get_assigned_resources()

    actors, bootstrap = [], None
    with tempfile.TemporaryDirectory(prefix="tp-ray-cpu-") as temp:
        report["private_temp"] = temp
        try:
            ray.init(address="local", num_cpus=8, num_gpus=0, resources={MARKER: 1},
                     include_dashboard=False, _node_ip_address="127.0.0.1",
                     _temp_dir=temp, namespace=f"cpu-admission-{case}",
                     object_store_memory=128 * 1024 * 1024, log_to_driver=True)
            report["cluster_resources"] = ray.cluster_resources()
            assert report["cluster_resources"]["CPU"] == 8
            assert report["cluster_resources"].get("GPU", 0) == 0
            # Real TaskRunner constructor only; never call run(), trainer.init() or fit().
            runner = TaskRunnerV1.remote()
            actors.append(runner)
            report["taskrunner_assigned"] = ray.get(runner.__ray_call__.remote(
                lambda _: ray.get_runtime_context().get_assigned_resources()), timeout=40)
            assert report["taskrunner_assigned"].get("CPU", 0) == 0
            bootstrap = Bootstrap.remote()
            actors.append(bootstrap)
            future = bootstrap.start.remote(OmegaConf.to_container(cfg.transfer_queue, resolve=True))
            if case == "original":
                end = time.monotonic() + 45
                while True:
                    table = placement_group_table()
                    storage_pgs = [pg for pg in table.values() if len(pg["bundles"]) == 8]
                    if storage_pgs:
                        break
                    if time.monotonic() >= end:
                        raise TimeoutError("Original TQ did not reach storage PG in 45 seconds")
                    time.sleep(0.1)
                pg = storage_pgs[0]
                assert pg["state"] == "PENDING" and sum(b["CPU"] for b in pg["bundles"].values()) == 8
                controller = ray.get_actor("TransferQueueController", namespace="transfer_queue")
                assigned = ray.get(controller.__ray_call__.remote(
                    lambda _: ray.get_runtime_context().get_assigned_resources()), timeout=10)
                assert assigned["CPU"] == 1
                assert ray.available_resources()["CPU"] == 7
                try:
                    ray.get(future, timeout=3)
                except ray.exceptions.GetTimeoutError:
                    pass
                else:
                    raise AssertionError("Original TQ unexpectedly admitted 8 storage CPUs beside controller")
                report.update(result="PASS_EXPECTED_INFEASIBLE", controller_assigned=assigned,
                              pending_storage_pg=pg, available=ray.available_resources())
            else:
                report["tq_bootstrap"] = ray.get(future, timeout=60)
                assert report["tq_bootstrap"]["controller"]["CPU"] == 1
                assert len(report["tq_bootstrap"]["storage"]) == units
                assert all(r["CPU"] == 1 for r in report["tq_bootstrap"]["storage"])
                # Exact training PG CPU shape; custom synthetic marker replaces GPU.
                pg = placement_group([{"CPU": cpus_per_gpu, MARKER: 1}], strategy="STRICT_PACK")
                ray.get(pg.ready(), timeout=15)
                # WorkerDict requests 1/3 GPU, implying Ray's specified-resource
                # default of 1 lifetime CPU, INSIDE the existing 3-CPU PG.
                worker = DefaultActorShape.options(
                    resources={MARKER: 1 / cpus_per_gpu},
                    scheduling_strategy=PlacementGroupSchedulingStrategy(pg, 0),
                ).remote()
                actors.append(worker)
                report["worker_dict_shape"] = ray.get(worker.ping.remote(), timeout=15)
                assert report["worker_dict_shape"]["CPU"] == 1
                stats = DefaultActorShape.options(num_cpus=0).remote()
                actors.append(stats)
                assert ray.get(stats.ping.remote(), timeout=15).get("CPU", 0) == 0
                node_id = ray.get_runtime_context().get_node_id()
                labels = ["agent_loop", "vllm_server", "load_balancer"] + [f"reward_{i}" for i in range(8)]
                shapes = [DefaultActorShape.options(name=f"shape_{label}",
                          scheduling_strategy=NodeAffinitySchedulingStrategy(node_id, soft=True)).remote()
                          for label in labels]
                actors.extend(shapes)
                assigned = ray.get([a.ping.remote() for a in shapes], timeout=45)
                assert all(r.get("CPU", 0) == 0 for r in assigned)
                assert ray.available_resources()["CPU"] == 2
                report.update(result="PASS_ADMISSION_ONLY", actor_shapes=dict(zip(labels, assigned, strict=True)),
                              available=ray.available_resources(), placement_groups=placement_group_table())
                # Prove that admission is still possible after every persistent reservation.
                probe = DefaultActorShape.options(num_cpus=1).remote()
                actors.append(probe)
                assert ray.get(probe.ping.remote(), timeout=10)["CPU"] == 1
                report["extra_one_cpu_actor_admitted"] = True
                ray.get(bootstrap.close.remote(), timeout=15)
        finally:
            if ray.is_initialized():
                for actor in actors:
                    ray.kill(actor, no_restart=True)
                for pg in placement_group_table().values():
                    from ray.util.placement_group import PlacementGroup
                    remove_placement_group(PlacementGroup(ray.PlacementGroupID.from_hex(pg["placement_group_id"])))
            children = psutil.Process().children(recursive=True)
            ray.shutdown()  # own local cluster only; never ray stop / global connect
            _, alive = psutil.wait_procs(children, timeout=10)
            live = [p.pid for p in alive if p.is_running() and p.status() != psutil.STATUS_ZOMBIE]
            report["cleanup"] = dict(ray_initialized=ray.is_initialized(), surviving_child_pids=live)
            signal.alarm(0)
            (work / f"{case}-result.json").write_text(json.dumps(report, indent=2, default=str))
            assert not live, f"Owned Ray subprocesses survived shutdown: {live}"
    assert not Path(report["private_temp"]).exists()
    report["cleanup"]["private_temp_removed"] = True
    (work / f"{case}-result.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str), flush=True)


if __name__ == "__main__":
    action, work_arg = sys.argv[1:3]
    work_dir = Path(work_arg).resolve()
    if action == "generate":
        generate(work_dir)
    elif action == "check":
        check(work_dir)
    elif action == "case":
        run_case(work_dir, sys.argv[3])
    else:
        raise ValueError(action)
