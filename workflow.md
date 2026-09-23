# Workflow — automated rollback for Docker images on AWS (ECS on EC2)

Everything this project does, everything you must set up, and every problem we
hit getting there. Written for someone picking the project up cold.

**Platform:** Amazon ECS with the **EC2 launch type** behind an Application Load
Balancer. (The project was originally built on Fargate and migrated to EC2 when
we learned production runs on EC2 — see [Appendix B](#appendix-b--the-fargate--ec2-migration).)

| | |
|---|---|
| AWS account | `010526241989` |
| Region | `eu-north-1` (Europe, Stockholm) |
| Repository | `EswarBSC/Automated-rollback-for-Docker-images-on-AWS` |

---

## 1. What this project proves

| # | Goal | How it is met |
|---|---|---|
| 1 | One-command rollback that redeploys a previously tested image **without rebuilding** | The Rollback workflow contains no `docker` command at all. It points the ECS service at a task definition revision that already exists. |
| 2 | Short-lived GitHub OIDC credentials, no permanently stored EC2 SSH key | GitHub assumes an IAM role via OIDC; credentials expire with the job. EC2 instances launch with **no key pair** and are reached through SSM Session Manager. |
| 3 | Rolling deployment with health checks and automated rollback | Rolling update at 100%/200% with two tasks behind an ALB, container + target-group health checks, and the ECS deployment circuit breaker set to roll back on failure. |

### The core idea

When a release goes wrong, the fastest and safest fix is to put back the **exact
image** that was working minutes ago — not a rebuild of the old commit.

A rebuild produces a **new artefact**: base image layers and transitive
dependencies can resolve differently today than last week, so "the same commit"
is not the same image. It also takes minutes and can fail again. During an
incident you want the opposite of novelty.

> **Roll back to a known artefact first. Fix forward afterwards, calmly.**

Three rules make that possible:

1. **Build once.** Every image is tagged with the 7-character git SHA. `:latest`
   appears nowhere in this repository.
2. **Only tested images ship.** Unit tests *and* a smoke test against the real
   running container must pass before `docker push`.
3. **Rollback never builds.** It only changes which task definition revision the
   service runs.

---

## 2. Architecture

```
Developer
   │ git push to main                        │ Actions → Run Rollback
   ▼                                          ▼
GitHub Actions  (authenticates to AWS with OIDC — no stored keys)
   ├── test    pytest
   ├── build   docker build --build-arg GIT_SHA
   │           smoke test /health on the real container
   │           docker push  <acct>.dkr.ecr.eu-north-1.amazonaws.com/rollback-demo:<sha>
   ├── deploy  register task definition revision
   │           save the OUTGOING revision to SSM Parameter Store
   │           update-service → poll rollout → verify
   └── rollback (separate workflow, NO BUILD)
               read target revision → update-service → poll → verify

AWS (eu-north-1)
   ECR  rollback-demo            :9d6a814  :89f0544  :c10b5cf  …
   Task definitions              rollback-demo-task:1 :2 :3 …   (each pins one image tag)
   ECS cluster                   rollback-demo-cluster   (EC2 capacity, ASG of 2+ instances)
   ECS service                   rollback-demo-service   (2 tasks, rolling update)
   ALB                           rollback-demo-alb → target group rollback-demo-tg
   SSM Parameter                 /rollback-demo/prod/previous-taskdef
   CloudWatch Logs               /ecs/rollback-demo
```

**Traffic path** — important for understanding the security groups:

```
browser → :80 → ALB            (security group sg-0ffdf2b1455cfea95)
                 │ :32768-65535   ← Docker's dynamic host port
                 ▼
          EC2 instance          (security group sg-0177b829839e4c23c)
                 │ :8000
                 ▼
          container (uvicorn)
```

**The rollback path never touches ECR.** That is the entire point of the project.

---

## 3. How a deploy works

Triggered by a push to `main` (excluding `**/*.md` and `docs/**`) or manually
via *Run workflow*.

### Job `test`
Installs `requirements-dev.txt`, runs `pytest -v`. If it fails, nothing else
runs — no image is built, nothing reaches AWS.

### Job `build` — skipped entirely if `vars.AWS_ROLE_ARN` is empty
1. Assume the AWS role via OIDC; log in to ECR.
2. `IMAGE_TAG = ${GITHUB_SHA:0:7}`.
3. **If that tag already exists in ECR, skip build and push.** ECR tags are
   immutable, so the existing image is byte-for-byte the tested one.
4. `docker build --build-arg GIT_SHA=<tag>` — the SHA is baked into the image.
5. **Smoke test:** run the container, poll `/health` for up to 30 seconds, fail
   if it never returns 200, remove the container.
6. Only then `docker push`.

> Unit tests prove the *code* is right. The smoke test proves the *image* boots
> and serves traffic — it catches bad Dockerfiles, missing files and wrong start
> commands that tests cannot see.

### Job `deploy`
1. `jq` replaces the `__IMAGE__` placeholder in `ecs/task-definition.json`.
2. `register-task-definition` → a new revision `rollback-demo-task:N`.
3. If the service does not exist or is not `ACTIVE`, print a `::warning::` and
   **exit successfully** — this is the expected first-run state before you have
   created the service.
4. Read the service's **current** revision. If it already equals the new one,
   skip.
5. **Write the current (outgoing) revision to the SSM parameter.** This is the
   rollback target, recorded *before* anything changes.
6. `update-service` to the new revision.
7. Poll every 15 s for up to 20 minutes, printing rollout state and the latest
   service event each time. Done when the primary deployment is `COMPLETED` and
   only one deployment remains; `FAILED` is detected too.
8. Verify the new revision is actually live. If ECS rolled back on its own, the
   run **fails** with:
   > *Deployment failed health checks; ECS automatically rolled back to
   > rollback-demo-task:N. Production kept serving the previous version.*
9. Write a summary table to the run page.

> A task definition revision is an immutable snapshot pinning one image tag.
> The numbered list of revisions **is** your deploy history, and rollback is
> choosing an earlier number.

**Why a custom polling loop instead of `aws ecs wait services-stable`:** the
built-in waiter times out at 10 minutes — too short for a blue/green bake — and
prints nothing while waiting. Ours prints progress and allows 20 minutes.

---

## 4. How a rollback works

Manual only (`workflow_dispatch`). Both inputs are optional:

| Input | Meaning |
|---|---|
| `revision` | `rollback-demo-task:5` or just `5`. **Leave empty** to use the revision saved in SSM by the last deploy. |
| `reason` | Free text, recorded in the run summary as an audit trail. |

1. Read the service's current task definition.
2. **Is a deployment still in flight?** If yes, call
   `stop-service-deployment --stop-type ROLLBACK` — ECS reverses its own rollout.
   This is the fastest option and the one that matters during a blue/green bake.
3. Otherwise resolve the target (input, or SSM), confirm the revision exists,
   and exit early if it is already live:
   *"already running the rollback target — nothing to do"*.
4. `update-service` to that revision, poll with the same loop, verify.
5. Write a summary: who triggered it, why, from → to (with image tags),
   duration, and **"No image was built during this rollback."**

**The SSM parameter is deliberately never written by this workflow**, so running
a rollback twice is harmless. Under incident pressure, people double-click.

**Separate concurrency group** (`rollback-prod` vs `deploy-prod`) so an
emergency rollback never queues behind the deploy it is undoing.

Terminal equivalent, same logic and same guarantees:

```bash
./scripts/rollback.sh          # use the SSM target
./scripts/rollback.sh 3        # roll back to rollback-demo-task:3
```

---

## 5. Repository layout

```
app/main.py                      FastAPI app. APP_COLOR + BANNER_MESSAGE at the top
                                 are marked "EDIT THESE FOR DEMOS".
                                 Env vars read at REQUEST time: GIT_SHA, APP_ENV,
                                 FAIL_HEALTH, SIMULATE_ERRORS.
                                 GET /        HTML banner page (500 if SIMULATE_ERRORS)
                                 GET /health  200 {"status":"ok"} (503 if FAIL_HEALTH)
                                 GET /version JSON build metadata
tests/test_app.py                5 pytest cases covering all of the above
conftest.py                      Empty; puts the repo root on sys.path for pytest
Dockerfile                       python:3.12-slim, non-root uid 10001,
                                 ARG GIT_SHA → ENV, uvicorn with 20s graceful shutdown
ecs/task-definition.json         EC2 task definition template, image = "__IMAGE__"
infra/github-actions-policy.json Least-privilege IAM policy for the OIDC role
infra/ecs-infrastructure-trust-policy.json   For blue/green later
infra/README.md                  Console click-by-click for every AWS resource
scripts/rollback.sh              Terminal rollback (bash, set -euo pipefail)
docs/DEMO.md                     Seven-scene presentation script
.github/workflows/deploy.yml     test → build → deploy
.github/workflows/rollback.yml   manual rollback, contains no build step
.github/workflows/debug-oidc.yml Diagnostic: prints the OIDC claims GitHub sends
```

### The EC2-specific settings in `ecs/task-definition.json`

| Setting | Value | Why |
|---|---|---|
| `requiresCompatibilities` | `["EC2"]` | An EC2 cluster rejects a Fargate-only task definition |
| `networkMode` | `bridge` | Standard for EC2. `awsvpc` works but gives each task its own ENI, capping tasks per instance |
| `hostPort` | **`0`** | Dynamic port mapping — see below |
| `memoryReservation` | `256` | Soft limit ECS uses to pack tasks onto instances; `memory` (512) stays the hard ceiling |
| `runtimePlatform` | *removed* | It pinned `X86_64` and would refuse to run on Graviton instances |
| `stopTimeout` | `30` | Must exceed uvicorn's 20 s graceful shutdown so in-flight requests finish |
| `healthCheck` | python + urllib against `/health` | interval 10, timeout 5, retries 3, startPeriod 15 |

> **`hostPort: 0` is the single most important EC2 setting.** With a fixed host
> port, only one task fits per instance — and a rolling update then **deadlocks**,
> because ECS cannot start the replacement while the old task still holds the
> port. The deployment sits at `IN_PROGRESS` until it times out. Dynamic ports
> let old and new tasks coexist on one instance for the few seconds of a rollout,
> which is exactly what zero downtime requires. The ALB discovers the real port
> automatically.
>
> This is also **why the EC2 setup needs an ALB**: with a random ephemeral port
> there is no fixed `host:port` for a browser to hit.

---

## 6. One-time AWS setup

Do these in order. IAM is global; everything else must be in **eu-north-1**.

### Part A — IAM (console shows "Global"; that is correct)

**A1. OIDC identity provider**
IAM → Identity providers → Add provider → **OpenID Connect**
- Provider URL: `https://token.actions.githubusercontent.com`
- Audience: `sts.amazonaws.com`

There is no longer a *Get thumbprint* button — AWS validates well-known
providers against its own trust store.

**A2. Permissions policy**
IAM → Policies → Create policy → JSON → paste `infra/github-actions-policy.json`
→ name `github-actions-rollback-demo-policy`.

What it allows and why:

| Statement | Purpose |
|---|---|
| `ecr:GetAuthorizationToken` on `*` | `docker login`. AWS does not support scoping this action |
| ECR push/pull + `DescribeImages` on the `rollback-demo` repo ARN | push the image, check whether a tag exists |
| `ecs:RegisterTaskDefinition`, `DescribeTaskDefinition`, `ListTaskDefinitions` on `*` | task definitions have no pre-creation ARN to scope against |
| `ecs:UpdateService`, `DescribeServices` on **one service ARN** | the deploy/rollback call. Cannot touch any other service |
| `ecs:ListServiceDeployments`, `DescribeServiceDeployments`, `StopServiceDeployment`, `DescribeServiceRevisions` | find and stop an in-flight deployment |
| `iam:PassRole` on `ecsTaskExecutionRole` **with** `iam:PassedToService = ecs-tasks.amazonaws.com` | registering a task definition hands that role to ECS. The condition stops it being passed to EC2 or Lambda — the classic privilege-escalation guard |
| `ssm:GetParameter`, `PutParameter` on `/rollback-demo/prod/*` | the rollback pointer |

Note what is **absent**: no `ecr:DeleteRepository`, no `ecs:DeleteService`, no
`iam:*`. A compromised workflow cannot delete your infrastructure.

**A3. The GitHub Actions role**
IAM → Roles → Create role → **Custom trust policy**:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::010526241989:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
      },
      "StringLike": {
        "token.actions.githubusercontent.com:sub":
          "repo:EswarBSC@296782642/Automated-rollback-for-Docker-images-on-AWS@1381449371:*"
      }
    }
  }]
}
```

Attach `github-actions-rollback-demo-policy`. Name it exactly
**`github-actions-rollback-demo`**.

⚠️ **The `@` numbers are mandatory here.** See
[Problem 2](#problem-2--oidc-denied-with-a-trust-policy-that-looked-perfect) —
this cost us the most time of anything in the project.

**A4. `ecsTaskExecutionRole`**
Usually already present. If not: Create role → AWS service → *Elastic Container
Service Task* → attach `AmazonECSTaskExecutionRolePolicy` → name it exactly
`ecsTaskExecutionRole`. Used by the ECS **agent** to pull images and ship logs,
not by application code.

### Part B — Regional resources (eu-north-1)

| Service | What | Notes |
|---|---|---|
| **ECR** → Create repository | `rollback-demo`, **tag immutability ENABLED** | Immutability is the basis of "the old image is exactly what we tested" |
| **CloudWatch** → Create log group | `/ecs/rollback-demo` | Without it, tasks die with `ResourceInitializationError` |
| **SSM** → Parameter Store → Create | `/rollback-demo/prod/previous-taskdef`, type **String**, value `rollback-demo-task:1` | Overwritten by every deploy |

### Part C — The EC2 cluster

ECS → Clusters → **Create cluster**
- **Cluster name:** `rollback-demo-cluster`
- **Infrastructure:** tick the EC2 option (labelled *Amazon EC2 instances* or
  *Fargate and Self-managed instances* depending on console version)
- **Auto Scaling group (ASG):** *Create new group*

| Field | Value | Why |
|---|---|---|
| Provisioning model | `On-demand` | Spot instances get reclaimed mid-demo |
| Container instance AMI | Amazon Linux 2023 (ECS optimized) | ECS agent preinstalled |
| EC2 instance type | `t3.small` | `t3.micro` is too tight for 2 tasks at 512 MB |
| EC2 instance role | `ecsInstanceRole` (create if absent) | Lets the instance join the cluster |
| Capacity min / max | `2` / `4` | **Rolling updates need spare capacity** |
| SSH key pair | **Proceed without a key pair** | See below |
| Root EBS volume | `30` GiB | |

**Networking for EC2 instances:** default VPC, all subnets, new security group
`rollback-demo-instances-sg`, **Auto-assign public IP: Turn on**.

Takes 3–5 minutes (it builds a CloudFormation stack). Then verify
**Cluster → Infrastructure → Container instances** shows 2 `ACTIVE` instances.
If it stays empty, the instance role is the cause 95% of the time.

#### No SSH key — SSM Session Manager instead (goal #2)

Attach **`AmazonSSMManagedInstanceCore`** to `ecsInstanceRole`. You then get a
shell via Systems Manager → Session Manager, or
`aws ssm start-session --target i-xxxx`, with no `.pem` file, no inbound port 22,
and no bastion. Access is IAM-authenticated, expires with the session, and is
logged in CloudTrail.

A `.pem` file is a long-lived credential that lives on somebody's laptop, gets
copied into a password manager, and outlives the person who created it. Same
class of problem as an AWS access key in CI:

| Long-lived secret | Replaced by |
|---|---|
| AWS access keys in GitHub | OIDC — tokens minted per job, expire with it |
| `.pem` SSH key for EC2 | SSM Session Manager — IAM-authenticated, per-session |

Two roles that are easy to confuse:

| Role | Assumed by | Job |
|---|---|---|
| `ecsInstanceRole` | the EC2 instance | join the cluster, run the ECS agent |
| `ecsTaskExecutionRole` | `ecs-tasks.amazonaws.com` | pull the image from ECR, write logs |

### Part D — The service and load balancer

First, get an EC2-compatible task definition: run **Actions → Deploy → Run
workflow**. It goes green with a ⚠ that the service is not ACTIVE — correct,
you have not created it yet. Note the revision number it registered.

Then ECS → cluster → Services → **Create**:

| Section | Field | Value |
|---|---|---|
| Service details | Task definition family / revision | `rollback-demo-task` / newest |
| | Service name | `rollback-demo-service` |
| Compute configuration | Compute options | **Launch type** |
| | Launch type | **`EC2`** |
| Deployment configuration | Service type | `Replica` |
| | **Desired tasks** | **`2`** |
| Deployment options | Min / Max running tasks | `100` / `200` |
| Deployment failure detection | ☑ **Use the Amazon ECS deployment circuit breaker** | |
| | ☑ **Rollback on failures** | required for Scene 5 |
| Load balancing | Load balancer type | **Application Load Balancer** → *Create a new load balancer* |
| | Name | `rollback-demo-alb` |
| | Container to load balance | `app 8000:8000` |
| | Listener | port `80`, HTTP |
| | Target group name | `rollback-demo-tg` |
| | **Health check path** | **`/health`** |
| | Deregistration delay | `30` (default 300 makes deploys feel slow) |

Leave Service Connect, Service discovery, VPC Lattice, Auto scaling, Task
placement, Volume and Tags untouched.

> **Desired tasks must be 2.** With one task, ECS must stop the old container
> before the new one is ready — downtime on every deploy, which is the exact
> opposite of goal #3.
>
> **Health check path must be `/health`, not `/`.** In Scene 6 you set
> `SIMULATE_ERRORS=true` so `/` returns 500 while `/health` still returns 200.
> That scenario only works if the ALB checks `/health`.

### Part E — Security groups ⚠️ the step everyone gets wrong

Two groups, **opposite** rules. Think of it as two doors: the internet knocks on
the ALB's door (80); the ALB knocks on the instances' door (a random high port).

| Group | Role | Required inbound rule |
|---|---|---|
| `sg-0ffdf2b1455cfea95` | **ALB** | `HTTP` · port `80` · source `0.0.0.0/0` |
| `sg-0177b829839e4c23c` | **instances** | `Custom TCP` · `32768-65535` · source **`sg-0ffdf2b1455cfea95`** |

Sourcing the instance rule from the ALB's security group (not a CIDR) means
containers only accept traffic that came through the load balancer — nobody can
reach a container directly.

Delete any leftover port-8000 rules on the instance group: with dynamic port
mapping **nothing ever listens on host port 8000**, so such a rule opens the
whole internet to a closed port.

**Console quirk:** you cannot convert an existing IPv4-CIDR rule into a
security-group-referenced rule. AWS rejects it with *"You may not specify a
referenced group id for an existing IPv4 CIDR rule."* **Delete** the old rule and
**Add rule** to create a fresh row instead.

---

## 7. GitHub configuration

**Settings → Secrets and variables → Actions → Variables tab.** These are
*variables*, not secrets — none is sensitive, and keeping them visible makes the
workflows readable. Nothing account-specific is hardcoded in any workflow file.

| Variable | Value |
|---|---|
| `AWS_REGION` | `eu-north-1` |
| `AWS_ROLE_ARN` | `arn:aws:iam::010526241989:role/github-actions-rollback-demo` |
| `ECR_REPOSITORY` | `rollback-demo` |
| `ECS_CLUSTER` | `rollback-demo-cluster` |
| `ECS_SERVICE` | `rollback-demo-service` |
| `TASK_FAMILY` | `rollback-demo-task` |
| `CONTAINER_NAME` | `app` |
| `SSM_PREVIOUS_PARAM` | `/rollback-demo/prod/previous-taskdef` |

If `AWS_ROLE_ARN` is **empty**, the AWS jobs are *skipped* rather than failed —
so the very first push to a fresh fork still shows a green build.

---

## 8. Verification — proving each goal

Use the **ALB DNS name** as the app URL throughout. It never changes, through
every deploy and rollback.

| # | Scene | Action | What proves the goal |
|---|---|---|---|
| 1 | Normal deploy | push any change | New SHA tag in ECR, new revision, ALB URL keeps serving |
| 2 | Bad release | set `APP_COLOR = "#dc2626"`, `BANNER_MESSAGE = "v2 — BROKEN RELEASE"` | Page turns red. Every gate passed — health checks cannot catch a business bug |
| 3 | **One-click rollback** | Actions → Rollback → empty revision | Page blue again; **no new image in ECR**; summary says *"No image was built during this rollback."* → **goal 1** |
| 3b | Run it twice | Rollback again | *"already running the rollback target — nothing to do"*, green → safe under pressure |
| 4 | CI blocks a build | break a test, push | `test` fails; `build` and `deploy` **skipped**; production untouched |
| 5 | **Circuit breaker** | `FAIL_HEALTH=true` in the task definition | Build passes (image is fine, *config* is broken). ECS kills unhealthy tasks, trips the breaker, restores the previous revision **by itself**. GitHub goes red; the ALB URL never stops serving → **goal 3** |
| 6 | Alarm rollback | `SIMULATE_ERRORS=true` | Deploy goes **green** (health check still 200) but every page is a 500. A human must roll back → why you need both defences |
| 7 | Blue/green | switch the deployment controller | Rollback during bake time is seconds, because the old version was never stopped |
| — | No SSH key | Session Manager shell into an instance | No `.pem`, no port 22 → **goal 2** |

Scene 5 is the one that actually proves automated rollback. Expect the circuit
breaker to take **5–15 minutes** — it deliberately tolerates several failed
launches before declaring failure. That is correct behaviour, not a hang.

**The line for scene 5:** *"A red pipeline and a healthy production system at the
same time — that is exactly what you want. The deployment failed; the service did
not. And CI was green, so this failure is caught by the platform, not by tests."*

---

## 9. Problems we hit, and the fixes

This section is the real value of the document. Every one of these cost real
time.

### Problem 1 — `git push` denied to the wrong GitHub account
`remote: Permission to EswarBSC/... denied to eswaroy` → HTTP 403.

**Cause:** Windows Credential Manager held a saved login
(`git:https://github.com` → `eswaroy`, a personal account) which Git Credential
Manager handed to every github.com push.

**Fix:** `cmdkey /delete:LegacyGeneric:target=git:https://github.com`, then pin
the repo to the company identity:

```bash
git remote set-url origin https://EswarBSC@github.com/EswarBSC/Automated-rollback-for-Docker-images-on-AWS.git
git config --local credential.username EswarBSC
git config --local credential.useHttpPath true      # store credentials per repo path
```

`useHttpPath` is the safety net: credentials are keyed to this repository's full
path, so a personal login elsewhere cannot silently take over again.

### Problem 2 — OIDC denied with a trust policy that looked perfect

```
Could not assume role with OIDC: Not authorized to perform sts:AssumeRoleWithWebIdentity
```

Everything checked out individually: the identity provider existed, its audience
was `sts.amazonaws.com`, the role name and ARN matched, the trust policy used
`sts:AssumeRoleWithWebIdentity`, and the `sub` read
`repo:EswarBSC/Automated-rollback-for-Docker-images-on-AWS:*` — exactly what
every guide online shows.

**AWS returns the same message whether the provider is missing, the role is
missing, or the condition does not match.** It will not tell you which.

**Cause:** the `EswarBSC` organization has GitHub's **unique token claims**
(immutable IDs) setting enabled. The real `sub` is:

```
repo:EswarBSC@296782642/Automated-rollback-for-Docker-images-on-AWS@1381449371:ref:refs/heads/main
```

`296782642` is the owner ID, `1381449371` the repository ID. `StringLike` on the
name-only form never matches.

**Fix:** put the long form in the trust policy. Keep it — it is *more* secure:
the IDs survive renames, and a deleted-and-recreated repository of the same name
gets a **different** ID, so nobody can hijack your AWS role by grabbing the name.

**How we found it:** `.github/workflows/debug-oidc.yml` requests the token and
prints the decoded `iss` / `aud` / `sub` claims (claims only, never the token).
If you fork this project into another repo or org, run it and copy your own
`sub`. Do not guess the numbers.

### Problem 3 — `Unknown parameter in input: "_comment"`
A JSON file cannot carry comments, and `aws ecs register-task-definition`
rejects any field it does not recognise — including a well-meaning `"_comment"`
key we added to document the file. Keep `ecs/task-definition.json` free of extra
fields; put the explanation in `infra/README.md` instead.

### Problem 4 — "Re-run failed jobs" replays the old commit
After pushing a workflow fix we kept seeing the *old* action versions in the
logs. **Re-run failed jobs replays the run at its original commit.** To pick up
new workflow code or new variable values, use
**Actions → Deploy → Run workflow ▾ → main**.

### Problem 5 — Repository variables are read when a run *starts*
We corrected `AWS_ROLE_ARN` and re-ran — still failing, because every existing
run had already captured the old value. Editing a variable retriggers nothing.
Start a **new** run.

### Problem 6 — ALB targets `unhealthy`, "Request timed out"
Targets registered on ports `32769` / `32770` (dynamic mapping working
correctly) but all health checks timed out.

**A timeout means the packet never arrived — it is always a security group.**
A broken application gives you *"Health checks failed with these codes: [500]"*
or a connection refused, not a timeout.

**Fix:** the instance security group only allowed port 8000, which nothing
listens on at host level. Add `Custom TCP 32768-65535` sourced from the **ALB's**
security group. See Part E.

### Problem 7 — `ERR_CONNECTION_TIMED_OUT` on the ALB hostname
We applied the ephemeral-port rule to the **ALB's** group instead of the
instances', leaving the ALB with no port 80 at all. Different hop, different
group — check which group is attached to which resource before editing:

- ALB's group: EC2 → Load Balancers → *Security* tab
- Instances' group: EC2 → Instances → *Security* tab

An `internal-` prefix in the ALB DNS name would mean an internal-scheme ALB
(another cause of this error). Ours has no prefix, so it is internet-facing.

### Problem 8 — `README.md` was UTF-16
`echo "# ..." >> README.md` in PowerShell writes UTF-16LE. Editors then show
`#\0 \0A\0u\0t\0…`. Converted to UTF-8 explicitly.

### Problem 9 — CRLF would break `rollback.sh`
Git for Windows sets `core.autocrlf=true` globally, which can commit CRLF into
shell scripts; they then fail on Linux with `bad interpreter: /bin/bash^M`.
Added `.gitattributes` with `* text=auto eol=lf` and explicit `eol=lf` for
`*.sh`, `Dockerfile`, `*.yml`, `*.json`, `*.py`, and marked the script executable
with `git update-index --chmod=+x scripts/rollback.sh`.

### Problem 10 — Node 20 deprecation warnings
`actions/checkout@v4` and friends target Node 20, which runners now force onto
Node 24. Bumped to `checkout@v7`, `setup-python@v7`,
`configure-aws-credentials@v6`; `amazon-ecr-login@v2` is still current.

---

## 10. Operational notes

### Cost
2 × `t3.small` ≈ $0.025/hr plus an ALB ≈ $0.023/hr ≈ **$1.20/day**. Unlike
Fargate these run whether or not you use them. Delete the cluster and ALB when
the demo is finished.

### Troubleshooting quick table

| Symptom | Cause |
|---|---|
| `build`/`deploy` jobs show as **skipped** | `AWS_ROLE_ARN` is empty — intended before AWS is set up |
| `Not authorized to perform sts:AssumeRoleWithWebIdentity` | Trust policy `sub` mismatch — see Problem 2; run `Debug OIDC` |
| `denied: ... ecr:InitiateLayerUpload` | ECR repo name or region does not match the policy ARN |
| Targets `unhealthy`, "Request timed out" | Instance SG missing `32768-65535` from the ALB SG |
| Browser times out on the ALB hostname | ALB SG missing inbound `80` |
| ALB returns 503 | No healthy targets |
| Tasks `PENDING` forever | No registered container instances, or not enough memory |
| `CannotPullContainerError` | Instances cannot reach ECR — public IP off, or no NAT/VPC endpoints |
| `ResourceInitializationError ... logs` | Log group `/ecs/rollback-demo` missing |
| Deploy hangs `IN_PROGRESS` then times out | Circuit breaker not enabled, or a fixed `hostPort` deadlocking the rollout |
| Rollback: *"SSM parameter is empty"* | No successful deploy yet — pass a revision explicitly |
| Deploy fails *"ECS automatically rolled back to …"* | Working as designed — the new version failed health checks |

### Deliberate scope limits
- **No Terraform or CDK.** AWS objects are created by hand once so a beginner can
  see exactly what exists and why.
- **No database**, so no schema-migration story. Rollback covers the application
  tier only; migrations must be backwards-compatible for *any* rollback strategy
  to work.
- **No automatic alarm→rollback wiring.** A human clicks the button and the
  reason is recorded. `docs/DEMO.md` explains how to automate it.

### A note on goal #2's wording
The goal says *"use AWS SSM to **execute deployment commands**"*, which describes
SSM **Run Command**. This pipeline calls the **ECS API**
(`register-task-definition`, `update-service`) and uses SSM only as Parameter
Store for the rollback pointer.

That is a stronger position, not a weaker one — **nothing logs into a server to
deploy at all** — but it is not literally what the sentence says. The honest
framing:

> *The goal behind that requirement was eliminating long-lived credentials. We
> removed them in both places: OIDC replaces AWS access keys in CI, and Session
> Manager replaces the SSH key on the host. The deployment itself never touches a
> server — it calls the ECS API.*

If SSM Run Command driving the deployment is a hard requirement, that is a
different pipeline (EC2 + Docker directly, no ECS) and should be chosen
deliberately.

---

## 11. Current state

**Committed on `main`:** `FAIL_HEALTH = "true"` in `ecs/task-definition.json` and
the red `v2 — BROKEN RELEASE` banner in `app/main.py`. This is the **Scene 5
configuration** — every deploy from this state is *supposed* to fail its health
checks and be rolled back by ECS.

To return to a healthy baseline:

```python
# app/main.py
APP_COLOR = "#2563eb"                    # blue
BANNER_MESSAGE = "v1 — stable release"
```
```json
// ecs/task-definition.json
{ "name": "FAIL_HEALTH", "value": "false" }
```

### Done
- Repository, app, tests, Dockerfile, both workflows, `rollback.sh`, docs
- IAM: OIDC provider, least-privilege policy, `github-actions-rollback-demo`
  (with the immutable-ID trust policy), `ecsTaskExecutionRole`, `ecsInstanceRole`
- ECR repository, CloudWatch log group, SSM parameter
- Deploy pipeline green end to end: tests → build → smoke test → push →
  register → update-service
- EC2 cluster with an ASG, ALB `rollback-demo-alb`, target group
  `rollback-demo-tg`, service `rollback-demo-service`

### Remaining to claim all three goals
1. Finish the two security-group rules (Part E) and confirm targets **healthy**
2. Confirm the ALB URL serves the banner page
3. Run a deploy while refreshing the ALB URL — it must never break → **goal 3a**
4. Run the Rollback workflow → page reverts, ECR unchanged → **goal 1**
5. Run Scene 5 (`FAIL_HEALTH=true`) → ECS rolls back unaided → **goal 3b**
6. Open a Session Manager session to an instance → **goal 2**

---

## Appendix A — Local development

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest -v
uvicorn app.main:app --reload      # http://localhost:8000
```

```bash
docker build --build-arg GIT_SHA=local-test -t rollback-demo:local-test .
docker run --rm -p 8000:8000 rollback-demo:local-test
```

Verified locally before anything reached AWS: 5/5 tests pass; the image builds;
`/health` returns 200 within 2 s; the container runs as `uid=10001(appuser)`;
`SIMULATE_ERRORS=true` gives HTTP 500; `FAIL_HEALTH=true` gives HTTP 503; the
task definition's health-check command exits 0 when healthy and non-zero on a
503; and the CI `jq` substitution replaces `__IMAGE__` leaving valid JSON.

## Appendix B — The Fargate → EC2 migration

The project was first built and proven on Fargate, then migrated when we learned
production runs on EC2. What that migration actually required:

**Changed — five lines of `ecs/task-definition.json`:**

| Setting | Fargate | EC2 |
|---|---|---|
| `requiresCompatibilities` | `["FARGATE"]` | `["EC2"]` |
| `networkMode` | `awsvpc` | `bridge` |
| `hostPort` | `8000` | `0` |
| `memoryReservation` | — | `256` |
| `runtimePlatform` | `LINUX/X86_64` | removed |

**Unchanged:** `deploy.yml` (one comment), `rollback.yml` (nothing),
`scripts/rollback.sh` (nothing), `infra/github-actions-policy.json` (nothing).

> Rollback is still "point the service at an older task definition revision", and
> `update-service` is identical on both launch types. **The rollback mechanism
> did not care that the entire compute platform changed underneath it.** That is
> worth saying out loud when presenting.

**Migration steps:** delete the service (launch type cannot be changed) → delete
the cluster → recreate the cluster with EC2 capacity → run Deploy to register an
EC2-compatible revision → create the service with launch type EC2 and an ALB →
fix the security groups.

**Also note:** old Fargate revisions (`rollback-demo-task:1`, `:2`) are
Fargate-only and are **no longer valid rollback targets**. The revision history
effectively restarts at the first EC2 revision.

---

# Additional improvements

Two gaps raised in review after the core project was signed off, and what was
built to close them. Both are live in the repository.

| # | Gap | Fix |
|---|---|---|
| 1 | Logs from every version were mixed together and unfilterable | Structured JSON logs carrying `version`, plus per-version log stream names |
| 2 | Nothing in GitHub showed which version was in production | GitHub Deployments API, a `What is live?` workflow, and a `Version logs` workflow |

---

## A1. Per-version log filtering

### The problem

All versions wrote to one log group with the stream prefix `app`, so every
stream was named `app/app/<task-id>`. Nothing identified the release. To read
"the logs for v2" you had to know which task IDs happened to be running at the
time — and after a rollback those tasks were gone.

Logs were also plain text, so nothing could be filtered or aggregated. You could
grep for a substring; you could not ask *"what was the error rate of the release
we just rolled back?"*

### The decision: keep ONE log group

A log group per release looks tidy and is a trap:

- retention, metric filters and alarms to maintain per release;
- log group sprawl that nobody cleans up;
- and worst, it makes the single most valuable incident query impossible —
  **comparing two releases side by side**.

So the version is attached to the *data* instead, in two independent ways.

### a) Structured JSON logs — `app/logging_config.py`

Every line is one JSON object carrying `version`, `env`, `host` and the request
fields:

```json
{"timestamp":"2026-09-23T11:43:48.226Z","level":"INFO","logger":"app",
 "message":"request","version":"44f67cf","env":"prod","host":"c491314d996b",
 "request_id":"0eed6985d58041eb","method":"GET","path":"/health",
 "status":200,"duration_ms":1.25,"client":"10.0.1.23"}
```

Design points that matter:

- **CloudWatch Logs Insights discovers JSON fields automatically**, so
  `filter version = "44f67cf"` works with no configuration at all.
- **uvicorn's own loggers are re-pointed at the same formatter.** One
  plain-text line would break the JSON parse for an entire query, so "every line
  is JSON" has to be a guarantee, not a hope.
- **One line per record.** CloudWatch treats a newline as a record boundary, so
  pretty-printed JSON would arrive as unparseable fragments.
- **Levels are meaningful:** 5xx becomes `ERROR`, 4xx becomes `WARNING`,
  otherwise `INFO`. That makes `filter level = "ERROR"` a real signal instead of
  noise.
- **`default=str` on the serialiser**, so an unexpected object can never crash
  the logger. A log call must not be able to take the application down.
- **No new dependency** — about 40 lines of stdlib rather than another pinned
  package to keep patched.

A request-logging middleware in `app/main.py` emits one line per request with
`method`, `path`, `status`, `duration_ms` and `request_id`, and logs a startup
line so every task has a definitive *"I am version X"* marker in its stream.

### b) Per-version log stream names

`ecs/task-definition.json` now carries a second placeholder:

```json
"awslogs-stream-prefix": "__VERSION__"
```

The Deploy workflow replaces it with the image tag using the same `jq` step that
fills in `__IMAGE__`. ECS names each stream `<prefix>/<container>/<task-id>`, so
streams become:

```
44f67cf/app/c2ecb15c9d624da08e991f489233fe72
44f67cf/app/95ff731631c24f378a3b2ecbb7c114d3
b2641be/app/3f2b1c...
```

Filtering streams by prefix in the console needs **no query language at all**,
which matters when somebody who does not know Insights has to look during an
incident.

> **Why two streams per version?** One per *task*. Desired count is 2, so each
> release runs two containers and each gets its own stream. Same version, same
> container name, different task IDs. That is the zero-downtime configuration
> working as intended — not duplication. To read a release across both tasks at
> once, use Insights rather than clicking streams.

### c) `X-App-Version` response header

Every response carries `X-App-Version` and `X-Request-Id`:

```
$ curl -I http://<alb-dns>/health
x-app-version: 44f67cf
x-request-id: 18b4578db7b34db4
```

The version with no AWS access at all, and a request id that ties a user's
complaint to exact log lines.

### Queries worth keeping

```
# everything from one release
fields @timestamp, level, message, method, path, status, duration_ms
| filter version = "44f67cf"
| sort @timestamp desc | limit 100

# errors only, across all releases
fields @timestamp, version, path, status, message
| filter level = "ERROR"
| sort @timestamp desc

# compare releases - the query that justifies a single log group
stats count(*) as requests,
      sum(status >= 500) as errors,
      avg(duration_ms) as avg_ms,
      pct(duration_ms, 95) as p95_ms
  by version
```

---

## A2. Knowing which version is live, from GitHub

Three layers, because each fails in a different way.

### a) GitHub Deployments API → the Environments panel

Both the Deploy and Rollback workflows write to the Deployments API, so the
repository home page shows a **production** environment with the live commit,
linked, with history. Also at `/deployments`.

**The design decision worth understanding:** we call the API *explicitly*
instead of using the `environment:` key on the job.

The job key always attributes the deployment to the SHA the workflow ran on.
That is correct for a deploy, and **wrong for a rollback**, where the commit
going live is an *older* one — the rollback workflow itself runs on the tip of
`main`, which is the broken version being removed. So the Rollback workflow
resolves the rolled-back image's tag back to its commit
(`gh api repos/.../commits/<tag>`) and registers *that* SHA.

Without this, the Environments panel would confidently show the wrong commit
after every single rollback.

Both bookkeeping steps use `continue-on-error: true`. Recording metadata must
never be able to fail a deployment or, far worse, a rollback.

### b) `What is live?` — `.github/workflows/status.yml`

**Actions → What is live? → Run workflow.** Read-only; changes nothing in AWS.

Queries ECS directly and reports the live revision, image, registered-at
timestamp, task counts, rollout state, the SSM rollback target, and the commit
that image tag maps to (subject, author, date, linked).

It also **compares AWS against GitHub's deployment record and flags drift**,
which is the part that earns its keep. Production can change without any
workflow:

- an ECS deployment circuit-breaker auto-rollback;
- `scripts/rollback.sh` run from a terminal;
- a console edit.

In all three cases GitHub's record silently goes stale. This workflow is what
catches it, which makes it the source of truth rather than a cached belief.

If `vars.APP_URL` is set it also asks the running application itself, via
`/version` and the `X-App-Version` header — the most direct evidence available.

### c) `Version logs` — `.github/workflows/logs.yml`

**Actions → Version logs → Run workflow.** Inputs: `version` (empty = whatever
is live), `minutes`, `level` (`ALL`/`WARNING`/`ERROR`), `limit`.

Runs the Logs Insights query and renders the results as a table in the job
summary, across every task of that release. Closes the loop: *which version is
live* and *what did it log* are both answerable from the Actions tab alone, with
no AWS console access.

---

## A3. What this required in AWS and GitHub

### One IAM policy update

`infra/github-actions-policy.json` gained two statements for the `Version logs`
workflow:

| Sid | Actions | Resource |
|---|---|---|
| `LogsInsightsQueryThisLogGroupOnly` | `logs:StartQuery`, `StopQuery`, `DescribeLogStreams`, `GetLogEvents`, `FilterLogEvents` | the `/ecs/rollback-demo` log group ARN |
| `LogsInsightsReadResults` | `logs:GetQueryResults` | `*` — AWS does not support resource-level permissions for this action |

Apply it: IAM → Policies → `github-actions-rollback-demo-policy` → **Edit** →
paste the updated file → Save. Until then only the `Version logs` workflow is
affected; everything else keeps working.

### Two new repository variables, both optional

| Variable | Value | Effect if unset |
|---|---|---|
| `LOG_GROUP` | `/ecs/rollback-demo` | Falls back to that exact default |
| `APP_URL` | `http://rollback-demo-alb-1358437757.eu-north-1.elb.amazonaws.com` | `status.yml` skips the "ask the app" section; deployment records carry no clickable link |

### Limits to be aware of

- **Existing log streams keep their old names.** CloudWatch cannot rename
  streams, so `app/app/<task-id>` entries from before this change stay as they
  are. Per-version naming applies from the next successful deploy onward.
- **The `version` field only appears** on lines written by an image built after
  this change.

---

## A4. Proven in production, by accident

The deploy that shipped these changes still had `FAIL_HEALTH: "true"` committed
from an earlier Scene 5 experiment. The result was an unplanned, completely real
demonstration of goal 3:

1. Unit tests passed and the smoke test passed — correctly. The *image* was
   fine; only the runtime *configuration* was broken, and the smoke test runs the
   image without that variable.
2. ECS launched the new tasks. Container health checks got 503. Tasks were
   killed, retried, killed again.
3. The deployment circuit breaker tripped and ECS restored
   `rollback-demo-task:7` **on its own**, with no human involved.
4. The deploy workflow's verify step caught it and failed the run:

   > *Deployment failed health checks; ECS automatically rolled back to
   > rollback-demo-task:7. Production kept serving the previous version.*

**A red pipeline and a healthy production system at the same time** — exactly
the intended outcome. The deployment failed; the service did not. And CI was
green throughout, which is the point: this class of failure is caught by the
platform, not by tests.

It also exercised the new logging on its first outing. That failed release's
streams are isolated under its own version prefix, with the 503s visible as
`"status": 503` on the request lines — both improvements demonstrated on a real
incident rather than a rehearsal.

---

## A5. Test coverage added

`tests/test_app.py` grew from 5 to 9 cases. The four new ones guard the logging
contract, because if it breaks the `Version logs` workflow silently returns
nothing rather than failing loudly:

| Test | Guards |
|---|---|
| `test_json_formatter_emits_one_line_of_json_with_the_version` | One line, valid JSON, carries `version` — the whole basis of per-version filtering |
| `test_json_formatter_never_raises_on_odd_values` | A log call cannot crash the app on a non-serialisable value |
| `test_responses_carry_the_version_header` | `X-App-Version` and `X-Request-Id` are present |
| `test_an_upstream_request_id_is_preserved` | An existing request id is honoured so traces join up |

Verified locally against a real container: every line is JSON including
uvicorn's, a 404 logs at `WARNING`, a 500 logs at `ERROR`, and `X-App-Version`
matches the build argument.
