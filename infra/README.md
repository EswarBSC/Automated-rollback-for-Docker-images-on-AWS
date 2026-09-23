# infra/ — the AWS pieces you create once, by hand

This folder holds the JSON documents you paste into the AWS console. Nothing in
here is applied automatically: there is no Terraform and no CDK in this project,
on purpose, so that a beginner can see exactly which AWS objects exist and why.

You set these up **once**. After that, every deploy and every rollback runs
through GitHub Actions without anyone touching the console.

| File | What it is | Where it goes |
|---|---|---|
| `github-actions-policy.json` | Least-privilege permissions policy for the GitHub Actions role | IAM → Policies → Create policy → JSON |
| `ecs-infrastructure-trust-policy.json` | Trust policy letting the ECS service itself assume a role | IAM → Roles → Create role → Custom trust policy |

Fixed values already filled in for you: account `010526241989`, region
`eu-north-1` (Europe, Stockholm).

---

## Set-up order

Do these in order — each step depends on the one before it.

### 1. ECR repository — where images live

**ECR → Repositories → Create repository**

- Visibility: **Private**
- Repository name: `rollback-demo`
- Tag immutability: **Enabled** ← important. It guarantees that tag `abc1234`
  can never be overwritten with different content, which is the whole basis of
  "the old image is still exactly what we tested".

### 2. CloudWatch log group — where container logs land

**CloudWatch → Log groups → Create log group**

- Name: `/ecs/rollback-demo` (must match `awslogs-group` in
  `../ecs/task-definition.json`)
- Retention: 1 week is plenty for a demo.

If this group does not exist, tasks fail to start with a
`ResourceInitializationError` about logging.

### 3. `ecsTaskExecutionRole` — lets ECS pull images and write logs

Most AWS accounts already have this role. Check **IAM → Roles → search
`ecsTaskExecutionRole`**. If it is missing:

**IAM → Roles → Create role**
- Trusted entity type: **AWS service**
- Use case: **Elastic Container Service** → **Elastic Container Service Task**
- Permissions: attach the AWS managed policy
  **`AmazonECSTaskExecutionRolePolicy`**
- Role name: exactly `ecsTaskExecutionRole`

This role is used by the ECS *agent* (to pull your image from ECR and ship logs
to CloudWatch), not by your application code.

### 4. GitHub OIDC identity provider — so GitHub needs no AWS keys

**IAM → Identity providers → Add provider**
- Provider type: **OpenID Connect**
- Provider URL: `https://token.actions.githubusercontent.com`
- Audience: `sts.amazonaws.com`

This is what replaces access keys. GitHub presents a short-lived, signed token
proving "I am a workflow running in repo X", and AWS trades it for temporary
credentials that expire when the job ends. Nothing long-lived is ever stored in
the repository.

### 5. The permissions policy — paste `github-actions-policy.json`

**IAM → Policies → Create policy → JSON tab**

Delete whatever is in the editor and paste the entire contents of
[`github-actions-policy.json`](github-actions-policy.json).

- Name: `github-actions-rollback-demo-policy`

What it allows, and why each piece is needed:

| Statement | Why the pipeline needs it |
|---|---|
| `EcrGetAuthorizationToken` | `docker login` to ECR. This action only works on `"*"` — AWS does not support scoping it. |
| `EcrPushAndPullThisRepositoryOnly` | Push the new image, and `DescribeImages` to check whether a tag already exists. Scoped to the `rollback-demo` repository only. |
| `EcsTaskDefinitions` | Register each new revision and read existing ones. Task definitions have no "pre-creation" ARN to scope against, so `"*"` is required here. |
| `EcsUpdateThisServiceOnly` | The actual deploy/rollback call. Scoped to exactly one service ARN — this role cannot touch any other service in the account. |
| `EcsDeploymentControl` | Lets the Rollback workflow find an in-flight deployment and stop it with `--stop-type ROLLBACK`. |
| `PassTaskExecutionRoleToEcsOnly` | Registering a task definition means handing `ecsTaskExecutionRole` to ECS. The condition `iam:PassedToService = ecs-tasks.amazonaws.com` means this role can only pass it to ECS, never to EC2 or Lambda — this is the classic privilege-escalation guard. |
| `SsmRollbackPointer` | Read and write `/rollback-demo/prod/previous-taskdef`, the saved rollback target. |

Note what is **absent**: no `ecr:DeleteRepository`, no `ecs:DeleteService`, no
`iam:*`. A compromised workflow cannot delete your infrastructure.

### 6. The GitHub Actions role — `github-actions-rollback-demo`

**IAM → Roles → Create role → Custom trust policy**

Paste this trust policy (it is repo-specific, which is why it is not a file in
this folder):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
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
          "token.actions.githubusercontent.com:sub": "repo:EswarBSC@296782642/Automated-rollback-for-Docker-images-on-AWS@1381449371:*"
        }
      }
    }
  ]
}
```

- Attach the policy `github-actions-rollback-demo-policy` from step 5.
- Role name: exactly `github-actions-rollback-demo`.
- Copy the role ARN — it becomes the GitHub variable `AWS_ROLE_ARN`.

The `sub` condition is the security boundary: only workflows in
**your** repository can assume this role. Without it, any GitHub repository in
the world could.

#### Why those `@` numbers are in the `sub` — read this before you copy a guide

Almost every tutorial online shows the `sub` as:

```
repo:OWNER/REPOSITORY:*
```

That form **does not work in the EswarBSC organization**, and produces a
baffling `Not authorized to perform sts:AssumeRoleWithWebIdentity` with a trust
policy that looks perfect. The organization has GitHub's **unique token claims**
(immutable IDs) setting enabled, so the token GitHub actually sends looks like:

```
repo:EswarBSC@296782642/Automated-rollback-for-Docker-images-on-AWS@1381449371:ref:refs/heads/main
```

`296782642` is the owner ID and `1381449371` is the repository ID. They never
change, even if the org or the repository is renamed — and, crucially, a
*deleted and recreated* repository of the same name gets a **different** ID. So
this form is strictly more secure than the name-based one: nobody can take over
your AWS role by grabbing your repository name after you delete it.

**If you fork this project into a different repo or org**, these numbers will be
wrong. Do not guess them — run the `Debug OIDC` workflow (in
`.github/workflows/`, delete it afterwards) and copy the `sub` it prints.

> Tightening it further: replace `:*` with
> `:ref:refs/heads/main` to allow only the main branch. Be aware that this also
> blocks `workflow_dispatch` runs from other branches, so do it after the demo
> works.

### 7. SSM parameter — the rollback pointer

**Systems Manager → Parameter Store → Create parameter**

- Name: `/rollback-demo/prod/previous-taskdef`
- Tier: Standard, Type: **String**
- Value: `rollback-demo-task:1` (any placeholder — the deploy workflow
  overwrites it on every deploy)

The deploy workflow writes the **last known good** revision here, and the
rollback workflow reads it. That one string is what makes rollback a single
click with no arguments.

"Last known good" rather than "the previous one" is deliberate. A revision is
only promoted once it has completed its rollout, run every task healthily,
suffered no task failures, and survived in production for `SOAK_MINUTES`
(default 15). Otherwise the pointer is left pointing at the last revision that
did. Without that rule, shipping a bad release and then shipping again would
overwrite the good pointer with the bad revision — and the one-click rollback
would take you to broken code.

The deploy workflow also maintains a second parameter alongside it,
`/rollback-demo/prod/known-good-history`, holding a JSON list of the last 10
promoted revisions. **You do not need to create this one** — it is written
automatically, and the IAM policy already covers it via the
`/rollback-demo/prod/*` wildcard. The Rollback workflow prints it in its summary
so an operator can walk further back if the newest known-good is also suspect.

---

## `ecs-infrastructure-trust-policy.json` — for blue/green later

This trust policy allows the **ECS service itself** (`ecs.amazonaws.com`) to
assume a role. It is different from every other role here:

- `ecsTaskExecutionRole` is assumed by `ecs-tasks.amazonaws.com` (the task).
- This one is assumed by `ecs.amazonaws.com` (the ECS control plane).

You need it when you switch the service to the **blue/green deployment
controller**, because ECS then has to reconfigure your load balancer listeners
on your behalf.

**To use it:** IAM → Roles → Create role → Custom trust policy → paste
[`ecs-infrastructure-trust-policy.json`](ecs-infrastructure-trust-policy.json)
→ attach the AWS managed policy
**`AmazonECSInfrastructureRolePolicyForLoadBalancers`** → name it
`ecsInfrastructureRole`. Then reference it as the service's *infrastructure
role* when configuring blue/green.

It is included now so the blue/green section of [`../docs/DEMO.md`](../docs/DEMO.md)
is not blocked on an IAM task.

---

## `ecsInstanceRole` — required because this runs on EC2, not Fargate

Fargate needs no instance role, because there are no instances. With the **EC2
launch type** every container instance runs the ECS agent, and that agent needs
permission to register itself with your cluster and report task state.

The **Create cluster** wizard creates this for you when you choose *Amazon EC2
instances* and let it build the Auto Scaling group. Verify afterwards:

**IAM → Roles → `ecsInstanceRole`** → it must have
**`AmazonEC2ContainerServiceforEC2Role`** attached.

If instances never appear under the cluster's **Infrastructure** tab, this role
is almost always the reason — the instance boots fine but cannot join the
cluster, so ECS has nowhere to place tasks and your deployment hangs forever.

Two distinct roles, easy to confuse:

| Role | Assumed by | Job |
|---|---|---|
| `ecsInstanceRole` | the EC2 instance | join the cluster, run the ECS agent |
| `ecsTaskExecutionRole` | `ecs-tasks.amazonaws.com` | pull the image from ECR, write logs |

Add at least **2 instances** to the Auto Scaling group. A rolling deployment
starts new tasks before stopping old ones, so it needs spare capacity somewhere.

### No SSH key — use SSM Session Manager instead

When the cluster wizard asks for an **EC2 key pair**, choose **"Proceed without
a key pair"**. Then add the AWS managed policy
**`AmazonSSMManagedInstanceCore`** to `ecsInstanceRole`.

That combination gives you shell access through **Systems Manager → Session
Manager** (or `aws ssm start-session --target i-xxxx`) with no `.pem` file, no
inbound port 22, and no bastion host. Access is authenticated by IAM, expires
with your session, and every command is auditable in CloudTrail.

This matters for the project's security goal. A `.pem` file is a long-lived
credential that lives on somebody's laptop, gets copied into a password manager,
and outlives the person who created it. It is the same class of problem as an AWS
access key in CI — which is why this project uses OIDC for GitHub. Same
principle, two places:

| Long-lived secret | Replaced by |
|---|---|
| AWS access keys in GitHub | OIDC — tokens minted per job, expire with it |
| `.pem` SSH key for EC2 | SSM Session Manager — IAM-authenticated, per-session |

Note what the deployment itself uses: neither SSH nor SSM Run Command. The
pipeline calls the **ECS API** (`register-task-definition`, `update-service`),
so nothing ever logs into a server to deploy. Session Manager is there for the
rare occasion a human needs to inspect an instance — not for releases.

---

## About `../ecs/task-definition.json`

That file is a **template**, not something you paste as-is:

- `"image": "__IMAGE__"` is a placeholder. The deploy workflow replaces it with
  the real ECR URI using `jq`, then registers the result.
- It contains no JSON comments and no extra keys, because
  `aws ecs register-task-definition` rejects any field it does not recognise —
  including a well-meaning `"_comment"`.

### The EC2-specific settings in it

| Setting | Value | Why |
|---|---|---|
| `requiresCompatibilities` | `["EC2"]` | Registering with `FARGATE` here would be rejected by an EC2-only cluster |
| `networkMode` | `bridge` | The normal EC2 mode. `awsvpc` also works but gives each task its own ENI, which caps how many tasks fit on an instance |
| `hostPort` | `0` | **Dynamic port mapping.** Docker picks a free ephemeral port |
| `memoryReservation` | `256` | Soft limit. ECS packs tasks onto instances using this, while `memory` stays the hard ceiling |
| `runtimePlatform` | *removed* | It pinned `X86_64`, which would refuse to run on Graviton instances. On EC2 the instance decides the architecture |

**Why `hostPort: 0` matters more than it looks.** With a fixed host port, only
one task can run per instance — and a rolling update then deadlocks, because ECS
cannot start the replacement task while the old one still holds the port. The
deployment sits at `IN_PROGRESS` until it times out. Dynamic ports let the old
and new task coexist on the same instance for the few seconds of a rollout, which
is exactly what "zero downtime" requires. The load balancer's target group
discovers the real port automatically.

This is also why the EC2 setup needs an **Application Load Balancer**: with a
random ephemeral port, there is no fixed `host:port` for a browser to hit.

If you ever register it manually (for the very first service creation), replace
`__IMAGE__` yourself with a real tag such as
`010526241989.dkr.ecr.eu-north-1.amazonaws.com/rollback-demo:abc1234`.
Never use `:latest` — a mutable tag makes it impossible to know what is running.
