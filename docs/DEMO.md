# Demo script — presenting this to seniors

Seven scenes, building from "a deploy works" to "production protects itself".
Each one has: what you do, what the audience sees, and the sentence that makes
the point. Total runtime ≈ 20 minutes.

**Before you start**

- Open four browser tabs: the app URL, the GitHub **Actions** tab, the ECS
  service page (**Deployments** and **Events**), and the ECR **Images** list.
- Have a terminal ready in the repo.
- Run one deploy the day before so `rollback-demo-task:1` exists, ECR has at
  least one image, and nothing is being built for the first time live.
- Know your app URL: the **load balancer's DNS name**. On the EC2 launch type
  the task definition uses dynamic port mapping (`hostPort: 0`), so there is no
  fixed `host:port` to browse — the ALB is the entry point, and it stays the
  same across every deploy and rollback. That stability is a feature: the
  audience watches one URL change content, never a URL that stops working.

Throughout, keep pointing at one thing: **the image tag**. Every scene is a
variation on "the tag that is live changed / did not change, and no build ran".

---

## Scene 1 — A normal deploy

**Do**

```bash
git commit --allow-empty -m "demo: normal deploy"
git push
```

**Narrate while it runs**

1. Actions → the **Deploy** workflow starts. Three jobs: `test`, `build`,
   `deploy`.
2. `test` runs pytest. Say: *"if any test fails, the build job never starts —
   nothing broken can even become an image."*
3. `build` builds the image tagged with the 7-char commit SHA, then runs the
   **smoke test**: it actually starts the container and polls `/health` until it
   returns 200. Say: *"unit tests prove the code is right; the smoke test proves
   the image boots. Only then do we push to ECR."*
4. `deploy` registers a new task definition revision, saves the **outgoing**
   revision to SSM, and switches the service over. The log prints the rollout
   state every 15 seconds.

**Show**

- ECR → a new image whose tag is the commit SHA. Scroll the list: *"every
  version we have ever shipped is still here."*
- ECS → Task definitions → `rollback-demo-task:2`, `:3`… *"this numbered list is
  our deploy history. Rollback is just picking an earlier number."*
- The run summary table, especially **Previous revision (rollback target)**.

**The line:** *"The deploy saved where we came from before it changed anything.
That is what makes the next scene a single click."*

---

## Scene 2 — Ship a bad release

You are about to break production on purpose. Say so.

**Do** — edit `app/main.py`:

```python
APP_COLOR = "#dc2626"                    # red
BANNER_MESSAGE = "v2 — BROKEN RELEASE"
```

```bash
git commit -am "demo: v2 with a visible regression"
git push
```

**Show**

Wait for the deploy to go green, then refresh the app. The page is **red** and
says *BROKEN RELEASE*.

**The line:** *"Every gate passed. Tests were green, the container was healthy,
ECS is perfectly happy — the app is simply wrong. Health checks cannot catch a
business bug. This is the case where you need a human rollback, and it is the
most common real-world outage."*

Leave the red page on screen. Let it be uncomfortable for a few seconds.

---

## Scene 3 — One-click rollback

**Do**

GitHub → **Actions** → **Rollback** → *Run workflow*:

- `revision`: **leave empty** (it will read the SSM parameter)
- `reason`: `v2 shipped the wrong banner`

**Narrate**

- The workflow reads the current revision, finds no deployment in flight, reads
  the rollback target from SSM, checks that revision really exists, and calls
  `update-service`.
- **Point at the job list: there is no build step. There is no docker command
  anywhere in this workflow.**

**Show**

- Refresh the app: **blue** again, *v1 — stable release*.
- The run summary: from revision + image tag → to revision + image tag,
  duration, and the line **"No image was built during this rollback."**
- ECR: *"no new image appeared. We are running the tag from Scene 1 — the exact
  bytes that were tested and that ran in production before."*

**The line:** *"Rollback took about a minute and could not have introduced a
surprise, because there is nothing new in it. A rebuild would pull fresh base
images and fresh transitive dependencies — that is a new artefact, and a new
artefact in the middle of an incident is a second risk on top of the first."*

**Bonus:** run the same workflow again. It prints *"already running the rollback
target — nothing to do"* and exits green. *"Safe to click twice under pressure,
because rollback never rewrites the saved pointer."*

**Terminal variant** (if someone asks about GitHub being unavailable):

```bash
./scripts/rollback.sh          # same logic, same guarantees
./scripts/rollback.sh 3        # or target a specific revision
```

---

## Scene 4 — CI blocks a broken build

Show that the pipeline refuses bad code before AWS is ever involved.

**Do** — break the app on purpose, e.g. in `app/main.py` change the health
handler to return the wrong shape:

```python
return JSONResponse(content={"state": "ok"}, status_code=200)   # key renamed
```

```bash
git commit -am "demo: break the health contract"
git push
```

**Show**

Actions → `test` fails. `build` and `deploy` are **skipped**, not run. ECR gets
no new image; ECS is untouched; production is still serving the good version.

**The line:** *"The cheapest rollback is the deploy that never happened. Tests
run in about twenty seconds; an ECS rollout takes minutes. Fail as early and as
cheaply as possible."*

> To demo the *smoke test* gate specifically, instead break the container start
> command in the `Dockerfile` (e.g. `app.main:application`). Unit tests pass,
> the image builds, and then the smoke test fails after 30 seconds of polling —
> so the push to ECR never happens. That is the gate that catches "works on my
> machine, does not start in a container".

Revert before the next scene:

```bash
git revert --no-edit HEAD && git push
```

---

## Scene 5 — Automatic rollback by the ECS circuit breaker

No human involved at all.

**Prerequisite (one-time):** ECS → service → Update → **Deployment failure
detection** → tick *Use the Amazon ECS deployment circuit breaker* and *Rollback
on failure*.

**Do** — edit `ecs/task-definition.json`:

```json
{ "name": "FAIL_HEALTH", "value": "true" }
```

```bash
git commit -am "demo: container health check will fail"
git push
```

**Show**

- ECS → Events, live. New tasks start, the container health check fails
  (`/health` returns 503), tasks are killed, ECS retries, then gives up.
- Rollout state goes `IN_PROGRESS` → `FAILED`, and ECS puts the previous task
  set back **by itself**.
- Refresh the app during all of this: it keeps working. Old tasks are only
  drained once new ones are healthy.
- The GitHub run goes **red**, with:
  *"Deployment failed health checks; ECS automatically rolled back to
  rollback-demo-task:N. Production kept serving the previous version."*

**The line:** *"A red pipeline and a healthy production system at the same time
— that is exactly what you want. The deployment failed; the service did not.
And note that the build was still green: this failure is caught by the
platform, not by CI."*

Set `FAIL_HEALTH` back to `"false"` and push.

---

## Scene 6 — Rollback triggered by an alarm

Some failures pass every health check. This is the 5xx case.

**Prerequisite (one-time):** a CloudWatch alarm on your target group's
`HTTPCode_Target_5XX_Count` (or on a metric filter over the `/ecs/rollback-demo`
log group if you have no load balancer). Threshold: > 5 in 1 minute.

**Do** — edit `ecs/task-definition.json`:

```json
{ "name": "SIMULATE_ERRORS", "value": "true" }
```

```bash
git commit -am "demo: 5xx errors, health check still passes"
git push
```

**Show**

- The deploy goes **green**: `/health` still returns 200, so ECS is satisfied.
- Refresh the app: every page view is a **500** error page.
- CloudWatch → the alarm flips to **In alarm** within a minute or two.
- Run the **Rollback** workflow, reason: `5xx alarm — RollbackDemo5xx`.
- The alarm returns to **OK**.

**The line:** *"Health checks only answer 'is the process up'. They cannot see
that every user request is failing. So the pipeline gives you two independent
lines of defence: ECS rolls back on unhealthy tasks automatically, and an alarm
plus a one-click rollback covers everything else. Both end at the same place —
an image that already existed."*

> If asked "can the alarm click the rollback for itself?": yes — attach the ECS
> deployment alarms feature (ECS watches named CloudWatch alarms during a
> deployment and rolls back if they fire), or have the alarm fire an SNS →
> Lambda that calls `repository_dispatch` on this workflow. Keeping a human in
> the loop is a deliberate choice for a demo, not a limitation.

Set `SIMULATE_ERRORS` back to `"false"` and push.

---

## Scene 7 — Blue/green: instant rollback during bake time

The strongest version of the story, if you have a load balancer configured.

**Prerequisite (one-time):** ECS → service → Update → deployment strategy
**Blue/green**, with a bake time of 5–10 minutes and the infrastructure role
created from
[`../infra/ecs-infrastructure-trust-policy.json`](../infra/ecs-infrastructure-trust-policy.json).

**Do**

Push any visible change (a new `BANNER_MESSAGE`). Watch the ECS **Deployments**
tab.

**Show**

- The green (new) task set starts alongside blue (old). Both are running.
- Traffic shifts to green, and the **bake time** begins — a window during which
  the old version is still standing by, fully warm.
- While it is baking, run the **Rollback** workflow.
- The workflow finds a deployment `IN_PROGRESS`, calls
  `stop-service-deployment --stop-type ROLLBACK`, and ECS shifts traffic
  straight back to blue.
- The app never goes down; the switch is a load balancer change, not a container
  start.

**The line:** *"This is the fastest possible rollback — seconds, because the old
version was never stopped. It is also why the deploy workflow waits up to 20
minutes instead of using the AWS CLI's built-in 10-minute waiter: with a bake
time, a healthy deploy legitimately takes longer than 10 minutes, and we must
not report success before the bake completes."*

---

## Closing slide — the four claims, and the evidence for each

| Claim | Evidence you just showed |
|---|---|
| Build once, deploy many | ECR has one image per commit SHA. Rollback pushed nothing. |
| Only tested images ship | Scene 4: `test` failed → `build` and `deploy` skipped. |
| Rollback is a pointer change | Scene 3: the Rollback workflow has no build step at all. |
| Production defends itself | Scene 5: ECS rolled back on its own; the app stayed up. |

**Numbers worth quoting**

- Rollback: ~1 minute (blue/green: seconds) vs. a full rebuild-and-deploy cycle.
- Rollback risk: zero new artefacts. A rebuild of "the same" commit can still
  pull different base-image layers and different transitive dependencies.
- AWS credentials stored in GitHub: **none**. OIDC issues short-lived tokens per
  job.

**Questions you should expect**

- *"Why not just `git revert` and redeploy?"* — That is a new build: new base
  image layers, new dependency resolution, several minutes, and a fresh chance
  to fail. Do it afterwards, calmly, to fix the code in `main`. Roll back first,
  fix forward second.
- *"What if the database schema changed?"* — Rollback covers the application
  tier. Schema changes must be backwards-compatible (expand/contract) for any
  rollback strategy to work; that is a design rule, not a pipeline feature.
- *"What if the previous version is also bad?"* — Pass an explicit older
  revision number to the workflow input. Every revision ever registered is still
  selectable.
- *"Who can trigger a rollback?"* — Anyone with write access to the repo; the
  actor and reason are recorded in the run summary. Add a GitHub Environment
  with required reviewers if you want approval gates.
