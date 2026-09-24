# follow.md — migrating the real system to meet the three targets

A step-by-step plan for changing a **live production system** from SSH-based
Docker Compose deploys to OIDC + SSM, with zero-downtime releases and a
one-command rollback that never rebuilds.

This is not the POC. The POC (see [`workflow.md`](workflow.md)) proved the
*principles* on ECS. This document is the plan for their actual estate, which
runs **EC2 + Docker Compose**.

> **Read this first.** Steps below contain placeholders in `ANGLE_BRACKETS` and
> assumptions marked **ASSUMPTION**. Phase 0 exists to replace every one of them
> with a fact. Do not start Phase 1 until Phase 0 is complete — half of the plan
> below may need adjusting once you see the real setup.

---

## 1. The gap

| # | Today | Target | Root cause to fix |
|---|---|---|---|
| 1 | Rollback = checkout old commit, rebuild image, redeploy | One command, redeploys a previously tested image, **no rebuild** | Images are built **on the instance** from source, so no reusable artefact exists |
| 2 | GitHub Actions holds a long-lived SSH private key | Short-lived OIDC credentials; SSM executes deployment commands | SSH key is a permanent credential in a CI secret |
| 3 | SSH/SCP + `docker compose up --build` → downtime | Rolling or blue/green, health checks, automated rollback | Containers are stopped before replacements are ready |

**The single most important change is #1's root cause.** Everything else
follows from it: once the image is built once in CI and stored in ECR with an
immutable tag, rollback becomes "run an older tag", zero-downtime becomes
"start the new tag alongside the old one", and the instance no longer needs
source code at all.

---

## 2. Principles for changing a live system

1. **Never big-bang.** Six phases, each independently useful and independently
   revertible. After every phase, production still works.
2. **The old path keeps working until the new one is proven.** SSH stays
   functional until Phase 6 — it is your escape hatch.
3. **Prove it somewhere else first.** Staging, or a clone of the prod instance.
   If neither exists, say so out loud and get a decision in writing.
4. **Every phase has an explicit rollback plan.** Written below.
5. **Agree a change window** for Phases 2, 3 and 6. Phases 1, 4 and 5 are
   additive and safe during business hours.
6. **Do not delete anything until the end.** Not the SSH key, not the old
   workflow, not port 22.

---

## 3. Phase 0 — Discovery (do this before writing any code)

You cannot plan accurately against a system you have not seen. Budget half a day.

### 3.1 Access and permissions

Confirm what your IAM user can actually do:

```bash
aws sts get-caller-identity                 # which account and principal am I?
aws iam list-attached-user-policies --user-name <YOUR_USER>
```

You will very likely **not** be allowed to create IAM roles. See §4 for what to
request.

### 3.2 The instances

```bash
# Which instances run the app?
aws ec2 describe-instances \
  --filters "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].{Id:InstanceId,Type:InstanceType,AZ:Placement.AvailabilityZone,PublicIp:PublicIpAddress,PrivateIp:PrivateIpAddress,Profile:IamInstanceProfile.Arn,Tags:Tags}' \
  --output table

# Are they managed by SSM already? (THE critical question for target 2)
aws ssm describe-instance-information \
  --query 'InstanceInformationList[].{Id:InstanceId,Ping:PingStatus,Agent:AgentVersion,Platform:PlatformName}' \
  --output table
```

**If an instance does not appear in `describe-instance-information`, SSM cannot
reach it.** Three possible causes, in order of likelihood:

| Cause | Check | Fix |
|---|---|---|
| No IAM instance profile, or missing `AmazonSSMManagedInstanceCore` | `IamInstanceProfile` above | Attach the policy (needs admin) |
| No network path to the SSM endpoints | Private subnet with no NAT? | NAT gateway, or VPC endpoints for `ssm`, `ssmmessages`, `ec2messages` |
| SSM Agent not installed or stopped | Log in and `systemctl status amazon-ssm-agent` | Install/start it. Preinstalled on Amazon Linux 2/2023 and Ubuntu 16.04+ |

### 3.3 Load balancing and networking

```bash
aws elbv2 describe-load-balancers --query 'LoadBalancers[].{Name:LoadBalancerName,DNS:DNSName,Scheme:Scheme,Type:Type}' --output table
aws elbv2 describe-target-groups   --query 'TargetGroups[].{Name:TargetGroupName,Port:Port,Protocol:Protocol,HealthPath:HealthCheckPath}' --output table
```

**Decision point:** is there an ALB in front of the instances, or does traffic
hit the instance directly (Elastic IP / DNS A record)? This changes the
zero-downtime design in Phase 3 — see §6.

### 3.4 On the instance

Connect **via Session Manager if it works** (this also proves SSM is usable):

```bash
aws ssm start-session --target <INSTANCE_ID>
```

Then gather:

```bash
# Where does the app live, and what runs it?
sudo docker ps
sudo docker compose version || docker-compose version
ls -la /opt/app /srv /home/*/app 2>/dev/null       # find the deploy directory
cat <DEPLOY_DIR>/docker-compose.yml

# Is there a reverse proxy?
systemctl status nginx caddy traefik 2>/dev/null
ls /etc/nginx/conf.d/ /etc/nginx/sites-enabled/ 2>/dev/null

# Resources — can this box run two copies of the app at once?
free -m ; df -h ; nproc

# Where does config/secrets come from?
ls -la <DEPLOY_DIR>/.env*                          # do NOT paste contents anywhere

# Is the image built here?
grep -n "build:" <DEPLOY_DIR>/docker-compose.yml
```

**Write down the answers.** Particularly:

- [ ] Deploy directory path
- [ ] Compose file uses `build:` (built on host) or `image:` (pulled)?
- [ ] Reverse proxy present? Which one? Config file paths?
- [ ] Free RAM — enough to run two copies simultaneously?
- [ ] Number of instances
- [ ] How secrets reach the container

### 3.5 The current pipeline

Read their existing workflow file end to end. Note:

- [ ] The GitHub secret name holding the SSH key
- [ ] Exact deploy commands (`scp` of what? `docker compose` with which flags?)
- [ ] Whether it runs migrations
- [ ] Whether any tests run before deploy
- [ ] Branch/trigger rules

### 3.6 The application

- [ ] **Does it have a health endpoint?** If not, **this is prerequisite work** —
      targets 1 and 3 both depend on it. A route returning 200 when the app can
      serve traffic (and ideally checking its DB connection).
- [ ] Is there a database? Are there migrations? Who runs them?
- [ ] Is the app stateless? Does it write to local disk?
- [ ] Startup time — how long from container start to serving?

### 3.7 Questions for the team

- What is the acceptable downtime today, and what do they want it to be?
- Is there a staging environment?
- Who approves production changes, and is there a change window?
- What is the rollback expectation — minutes? seconds?
- Any compliance/audit requirement on who deployed what?

---

## 4. What to ask the AWS admin for

You will need these. Ask once, in one message, with reasons — it is faster than
discovering them one at a time.

**Permissions for your own IAM user** (to build and operate the pipeline):

```
ecr:*                      on the new repository only
ssm:SendCommand, GetCommandInvocation, ListCommandInvocations
ssm:GetParameter, PutParameter, DescribeParameters   on /<app>/prod/*
ssm:CreateDocument, UpdateDocument, DescribeDocument, GetDocument
ssm:DescribeInstanceInformation, StartSession
ec2:DescribeInstances
cloudwatch:DescribeAlarms, GetMetricStatistics, PutMetricAlarm
logs:DescribeLogGroups, GetLogEvents, StartQuery, GetQueryResults
iam:PassRole                (only if you create roles yourself)
```

**Things only an admin can create** (list these explicitly):

1. **GitHub OIDC identity provider** — `token.actions.githubusercontent.com`,
   audience `sts.amazonaws.com`. One per account; may already exist.
2. **IAM role `github-actions-<app>-deploy`** with the trust policy in §11.1 and
   the permissions policy in §11.2.
3. **Attach `AmazonSSMManagedInstanceCore`** to the EC2 instance profile
   (plus ECR read — §11.3). If the instances have **no** instance profile at
   all, one must be created and attached (this requires a brief instance
   association change, not a restart).
4. **ECR repository** with tag immutability enabled, if you cannot create it.

> **Get the trust policy's `sub` claim right.** If their GitHub org has
> "unique token claims" enabled, the `sub` contains numeric IDs
> (`repo:Org@123/Repo@456:*`) and the name-only form silently fails with a
> misleading error. Run the `Debug OIDC` workflow from the POC repo to print the
> real claims before writing the trust policy. This cost a full afternoon on the
> POC.

---

## 5. Target architecture

```
GitHub Actions                                AWS
──────────────                                ───
push to main
  │
  ├─ test          pytest / npm test
  ├─ build         docker build --build-arg GIT_SHA=<sha>
  │                smoke-test the image in CI
  │                docker push  ──────────────►  ECR  <app>:<7-char-sha>
  │                                                     (immutable tags)
  └─ deploy        aws ssm send-command  ──────►  SSM Document: <App>-Deploy
                   (OIDC creds, 1 hour max)        parameter: ImageTag
                          │                              │
                          │                              ▼
                          │                        EC2 instance
                          │                          ├─ docker pull <tag>
                          │                          ├─ start INACTIVE colour
                          │                          ├─ health-check it locally
                          │                          ├─ switch nginx upstream
                          │                          ├─ nginx -s reload  ← ~0 downtime
                          │                          └─ stop old colour after drain
                          ▼
                   poll GetCommandInvocation, stream output

State in SSM Parameter Store
   /<app>/prod/current-tag           what is live now
   /<app>/prod/current-colour        blue | green
   /<app>/prod/last-known-good-tag   the rollback target
   /<app>/prod/known-good-history    JSON list, last 10

Rollback workflow = same SSM document, ImageTag = last-known-good.
No build. No source code on the instance. No SSH.
```

**Blue/green on one host.** Two Compose projects, `<app>-blue` on port 8081 and
`<app>-green` on 8082, with nginx on 80/443 proxying to whichever is active.
Switching is an `nginx -s reload`, which is graceful — in-flight requests finish
on the old worker.

Why this beats the ECS rolling update for rollback speed: if you leave the old
colour **running** after a switch, rolling back is a config file write plus a
reload — **sub-second**, with no container start at all.

**If there is an ALB in front (from §3.3)**, you have a second option: two target
groups and a listener rule switch. Also near-instant. Choose based on what they
already run; the nginx approach works either way and does not require ALB
permissions.

---

## 6. Phase 1 — Build once, push to ECR (keep SSH deploying)

**Goal:** an immutable, reusable artefact exists. This is the change that makes
targets 1 and 3 possible.
**Risk:** low. The deploy path is untouched.
**Downtime:** none.

### 6.1 Create the ECR repository

```bash
aws ecr create-repository \
  --repository-name <APP_NAME> \
  --image-tag-mutability IMMUTABLE \
  --image-scanning-configuration scanOnPush=true \
  --region <REGION>
```

Tag immutability is not optional — it is what guarantees that tag `abc1234`
always contains exactly the code of commit `abc1234`.

### 6.2 Set up OIDC (admin creates the role — §4, §11)

Add repository **variables** (not secrets — none of this is sensitive):

| Variable | Value |
|---|---|
| `AWS_REGION` | `<REGION>` |
| `AWS_ROLE_ARN` | `arn:aws:iam::<ACCOUNT>:role/github-actions-<app>-deploy` |
| `ECR_REPOSITORY` | `<APP_NAME>` |
| `SSM_DOCUMENT` | `<App>-Deploy` (used from Phase 2) |
| `INSTANCE_TAG_KEY` / `INSTANCE_TAG_VALUE` | e.g. `Application` / `<app>` |
| `SSM_PARAM_PREFIX` | `/<app>/prod` |
| `APP_URL` | their public URL |
| `SOAK_MINUTES` | `15` |

### 6.3 Add a build job to the existing workflow

Do **not** replace their deploy job yet. Add a job in front of it:

```yaml
permissions:
  contents: read
  id-token: write

jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      image: ${{ steps.meta.outputs.image }}
      tag:   ${{ steps.meta.outputs.tag }}
    steps:
      - uses: actions/checkout@v7
      - uses: aws-actions/configure-aws-credentials@v6
        with:
          role-to-assume: ${{ vars.AWS_ROLE_ARN }}
          aws-region: ${{ vars.AWS_REGION }}
      - id: ecr
        uses: aws-actions/amazon-ecr-login@v2
      - id: meta
        run: |
          TAG="${GITHUB_SHA:0:7}"
          echo "tag=$TAG" >> "$GITHUB_OUTPUT"
          echo "image=${{ steps.ecr.outputs.registry }}/${{ vars.ECR_REPOSITORY }}:$TAG" >> "$GITHUB_OUTPUT"
      - run: docker build --build-arg GIT_SHA=${{ steps.meta.outputs.tag }} -t ${{ steps.meta.outputs.image }} .
      # Smoke test: prove the IMAGE boots, not just that the code compiles.
      - run: |
          docker run -d --name smoke -p 8080:<APP_PORT> ${{ steps.meta.outputs.image }}
          for i in $(seq 1 30); do
            [ "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/health || true)" = "200" ] && ok=1 && break
            sleep 1
          done
          docker logs smoke; docker rm -f smoke
          [ "${ok:-0}" = "1" ] || { echo "::error::image failed to serve /health"; exit 1; }
      - run: docker push ${{ steps.meta.outputs.image }}
```

### 6.4 Change the Compose file to pull, not build

On the instance, `<DEPLOY_DIR>/docker-compose.yml`:

```yaml
# BEFORE
services:
  app:
    build: .
    ports: ["80:<APP_PORT>"]

# AFTER
services:
  app:
    image: ${APP_IMAGE}            # full ECR URI including the tag
    ports: ["${HOST_PORT}:<APP_PORT>"]
    restart: unless-stopped
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS http://localhost:<APP_PORT>/health || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 20s
```

### 6.5 Let the instance pull from ECR

Instance profile needs ECR read (§11.3). Then, on the instance:

```bash
aws ecr get-login-password --region <REGION> \
  | sudo docker login --username AWS --password-stdin <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com
```

ECR auth tokens last 12 hours, so the deploy script must log in every time — it
does, in §7.3.

### 6.6 Verify Phase 1

- [ ] A push produces an image in ECR tagged with the commit SHA
- [ ] `docker pull` of that tag works **from the instance**
- [ ] Their existing SSH deploy still works unchanged
- [ ] The smoke test fails the build when you deliberately break the start command

**Rollback for this phase:** revert the workflow change. Nothing else moved.

---

## 7. Phase 2 — Replace SSH with SSM Run Command

**Goal:** target 2. No SSH key, no port 22, no permanent credential.
**Risk:** medium — the deploy mechanism changes.
**Downtime:** same as today (blue/green comes in Phase 3).

### 7.1 Confirm SSM works before changing anything

```bash
aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --targets "Key=tag:<INSTANCE_TAG_KEY>,Values=<INSTANCE_TAG_VALUE>" \
  --parameters 'commands=["echo hello from $(hostname)"]' \
  --query 'Command.CommandId' --output text
```

Then read the result:

```bash
aws ssm list-command-invocations --command-id <CID> --details \
  --query 'CommandInvocations[].{Instance:InstanceId,Status:Status,Output:CommandPlugins[0].Output}'
```

If this fails, go back to §3.2 — nothing downstream can work until it succeeds.

### 7.2 Why a custom SSM document, not `AWS-RunShellScript`

Granting CI permission to run `AWS-RunShellScript` is granting **arbitrary root
command execution** on your production instances. It replaces an SSH key with
something equally powerful.

Instead, create **one custom document** containing the deploy script, taking
only an image tag as a parameter, and restrict the CI role to *that document*.
CI can then deploy — and nothing else. This is the strongest version of the
security story for target 2, and worth saying explicitly to your seniors.

### 7.3 Create the deploy document

Save as `ssm/deploy-document.yaml` in the repo:

```yaml
schemaVersion: '2.2'
description: Build-free blue/green deploy of <APP_NAME> from ECR.
parameters:
  ImageTag:
    type: String
    description: 7-char git SHA of an image already in ECR
    allowedPattern: '^[0-9a-f]{7,40}$'
  Action:
    type: String
    default: deploy
    allowedValues: [deploy, switch-back, status]
mainSteps:
  - action: aws:runShellScript
    name: deploy
    inputs:
      timeoutSeconds: '900'
      runCommand:
        - |
          set -euo pipefail
          ACTION="{{ Action }}"
          TAG="{{ ImageTag }}"
          REGION="<REGION>"
          ACCOUNT="<ACCOUNT>"
          REPO="<APP_NAME>"
          APP_PORT=<APP_PORT>
          DEPLOY_DIR="<DEPLOY_DIR>"
          PARAM_PREFIX="/<app>/prod"
          IMAGE="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${REPO}:${TAG}"
          UPSTREAM_FILE="/etc/nginx/conf.d/app_upstream.conf"

          cd "$DEPLOY_DIR"

          current_colour() {
            aws ssm get-parameter --region "$REGION" --name "${PARAM_PREFIX}/current-colour" \
              --query Parameter.Value --output text 2>/dev/null || echo "blue"
          }
          port_for() { [ "$1" = "blue" ] && echo 8081 || echo 8082; }

          switch_nginx() {   # $1 = port
            echo "upstream app_backend { server 127.0.0.1:$1; keepalive 32; }" > "$UPSTREAM_FILE"
            nginx -t
            nginx -s reload      # graceful: in-flight requests finish on old workers
          }

          CUR="$(current_colour)"
          NEW=$([ "$CUR" = "blue" ] && echo green || echo blue)
          CUR_PORT="$(port_for "$CUR")"
          NEW_PORT="$(port_for "$NEW")"

          if [ "$ACTION" = "status" ]; then
            # NOTE: deliberately no "docker --format" anywhere in this document.
            # SSM performs its own double-curly-brace substitution, so a Go
            # template collides with it and document creation fails. See the
            # warning below this code block.
            echo "colour=$CUR port=$CUR_PORT"; docker ps; exit 0
          fi

          if [ "$ACTION" = "switch-back" ]; then
            # Fast path: the previous colour is still running. Sub-second rollback.
            if [ -n "$(docker compose -p "${REPO}-${NEW}" ps -q 2>/dev/null)" ]; then
              switch_nginx "$NEW_PORT"
              aws ssm put-parameter --region "$REGION" --name "${PARAM_PREFIX}/current-colour" \
                --type String --value "$NEW" --overwrite >/dev/null
              echo "Switched traffic back to $NEW (no container start)."; exit 0
            fi
            echo "Previous colour is not running; use Action=deploy with the target tag."; exit 1
          fi

          echo "Deploying ${IMAGE} into the ${NEW} slot (currently serving ${CUR})"

          aws ecr get-login-password --region "$REGION" \
            | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
          docker pull "$IMAGE"

          APP_IMAGE="$IMAGE" HOST_PORT="$NEW_PORT" \
            docker compose -p "${REPO}-${NEW}" up -d --remove-orphans

          echo "Health-checking 127.0.0.1:${NEW_PORT}/health ..."
          ok=false
          for i in $(seq 1 60); do
            if [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${NEW_PORT}/health" || true)" = "200" ]; then
              ok=true; echo "healthy after ${i}s"; break
            fi
            sleep 1
          done

          if [ "$ok" != "true" ]; then
            echo "FAILED health check - tearing the new colour down. Production was never switched."
            docker compose -p "${REPO}-${NEW}" logs --tail 100 || true
            docker compose -p "${REPO}-${NEW}" down || true
            exit 1
          fi

          switch_nginx "$NEW_PORT"

          aws ssm put-parameter --region "$REGION" --name "${PARAM_PREFIX}/current-colour" \
            --type String --value "$NEW" --overwrite >/dev/null
          aws ssm put-parameter --region "$REGION" --name "${PARAM_PREFIX}/current-tag" \
            --type String --value "$TAG" --overwrite >/dev/null

          echo "Traffic now on ${NEW} (${TAG}). Previous colour ${CUR} left RUNNING for instant rollback."
          docker image prune -f >/dev/null 2>&1 || true
```

> **Gotcha that will bite you:** SSM does its own `{{ ... }}` substitution on the
> whole document and rejects any reference that is not a declared parameter. So
> Docker's Go templates — `docker ps --format '{{.Names}}'`, `{{.Status}}` and
> friends — make `create-document` fail with a confusing error. Use
> `docker compose -p <project> ps -q` instead, as above. The same applies to
> anything else using double curly braces inside the script.

> **Note the deliberate choice:** the old colour is **not** stopped. That is what
> makes `switch-back` sub-second. It costs one extra container's worth of RAM —
> check §3.4 that the box can take it. If it cannot, add a
> `docker compose -p "${REPO}-${CUR}" down` after a drain sleep, and accept that
> rollback then takes a container start (~10–30s, still no rebuild).

Register and update it:

```bash
aws ssm create-document --name "<App>-Deploy" --document-type Command \
  --document-format YAML --content file://ssm/deploy-document.yaml

# later changes:
aws ssm update-document --name "<App>-Deploy" --document-format YAML \
  --content file://ssm/deploy-document.yaml --document-version '$LATEST'
aws ssm update-document-default-version --name "<App>-Deploy" --document-version <N>
```

### 7.4 Prepare nginx on the instance

```bash
sudo tee /etc/nginx/conf.d/app_upstream.conf <<'EOF'
upstream app_backend { server 127.0.0.1:8081; keepalive 32; }
EOF

sudo tee /etc/nginx/conf.d/app.conf <<'EOF'
server {
    listen 80;
    server_name _;
    location / {
        proxy_pass http://app_backend;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
EOF

sudo nginx -t && sudo systemctl reload nginx
```

**ASSUMPTION:** nginx is already the front door. If the container currently
binds port 80 directly, introducing nginx is itself a small change — do it in
its own maintenance window, before Phase 3.

### 7.5 Swap the workflow's deploy job to SSM

```yaml
  deploy:
    needs: build
    runs-on: ubuntu-latest
    permissions: { contents: read, id-token: write }
    steps:
      - uses: aws-actions/configure-aws-credentials@v6
        with:
          role-to-assume: ${{ vars.AWS_ROLE_ARN }}
          aws-region: ${{ vars.AWS_REGION }}

      - name: Send the deploy command
        id: send
        run: |
          set -euo pipefail
          CID="$(aws ssm send-command \
            --document-name "${{ vars.SSM_DOCUMENT }}" \
            --targets "Key=tag:${{ vars.INSTANCE_TAG_KEY }},Values=${{ vars.INSTANCE_TAG_VALUE }}" \
            --parameters "ImageTag=${{ needs.build.outputs.tag }},Action=deploy" \
            --comment "Deploy ${{ needs.build.outputs.tag }} by ${GITHUB_ACTOR}" \
            --cloud-watch-output-config CloudWatchOutputEnabled=true,CloudWatchLogGroupName=/ssm/<app>-deploy \
            --query 'Command.CommandId' --output text)"
          echo "cid=$CID" >> "$GITHUB_OUTPUT"

      - name: Wait for it and stream the output
        run: |
          set -euo pipefail
          CID="${{ steps.send.outputs.cid }}"
          for _ in $(seq 1 120); do          # up to 20 minutes
            STATUS="$(aws ssm list-command-invocations --command-id "$CID" --details \
              --query 'CommandInvocations[0].Status' --output text)"
            case "$STATUS" in
              Pending|InProgress|Delayed) sleep 10 ;;
              Success) break ;;
              *) echo "::error::SSM command ended: $STATUS" ;;
            esac
          done
          aws ssm list-command-invocations --command-id "$CID" --details \
            --query 'CommandInvocations[].CommandPlugins[].Output' --output text
          [ "$STATUS" = "Success" ] || exit 1
```

> `GetCommandInvocation` truncates output at ~24,000 characters. The
> `--cloud-watch-output-config` flag above ships the full output to CloudWatch
> Logs — set that log group up, you will want it the first time something fails.

### 7.6 Verify Phase 2

- [ ] A push deploys end to end with **no SSH key used**
- [ ] The SSM command output appears in the Actions log
- [ ] Full output is in CloudWatch Logs
- [ ] `aws ssm start-session` gives you a shell when you need one
- [ ] Deliberately break the image → the command fails → **traffic never
      switched** and the old container is still serving

**Rollback for this phase:** re-enable the old SSH deploy job. Do not delete
the SSH key yet.

---

## 8. Phase 3 — Zero downtime

If you implemented §7.3 as written, **you already have this**: the new colour is
started and health-checked before nginx is switched, and the reload is graceful.

What remains is proving it:

```bash
# from your laptop, during a deploy
while true; do
  curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" https://<APP_URL>/health
  sleep 0.2
done
```

Deploy while that runs. **Every line must be `200`.** A single `502`/`000` means
something is wrong — most likely nginx switching before the app is ready, or the
app not draining in-flight requests on shutdown.

- [ ] 5 minutes of continuous polling across a deploy, zero non-200 responses
- [ ] Repeat with a deliberately broken image → still zero non-200, because the
      switch never happens

**Also required for genuine zero downtime:** the app must handle `SIGTERM`
gracefully (finish in-flight requests, then exit). Check this — many apps do not.
In Compose, `stop_grace_period: 30s` gives it time.

---

## 9. Phase 4 — One-command rollback

**Goal:** target 1.

### 9.1 Track the last known good tag

Reuse the rule proven in the POC (`workflow.md` §A6): a tag is promoted to
*last known good* only after it has been live and healthy for `SOAK_MINUTES`.
Saving "the previous tag" is a trap — deploy bad, deploy again, and your
rollback target *is* the bad one.

Add to the deploy workflow, before switching:

```yaml
      - name: Promote the outgoing tag if it proved itself
        run: |
          set -euo pipefail
          P="${{ vars.SSM_PARAM_PREFIX }}"
          CUR="$(aws ssm get-parameter --name "$P/current-tag" --query Parameter.Value --output text 2>/dev/null || echo '')"
          SINCE="$(aws ssm get-parameter --name "$P/current-since" --query Parameter.Value --output text 2>/dev/null || echo 0)"
          AGE_MIN=$(( ( $(date +%s) - ${SINCE:-0} ) / 60 ))
          HEALTHY="$(curl -s -o /dev/null -w '%{http_code}' "${{ vars.APP_URL }}/health" || echo 000)"

          if [ -n "$CUR" ] && [ "$HEALTHY" = "200" ] && [ "$AGE_MIN" -ge "${{ vars.SOAK_MINUTES }}" ]; then
            aws ssm put-parameter --name "$P/last-known-good-tag" --type String --value "$CUR" --overwrite >/dev/null
            echo "::notice::Promoted $CUR to last known good (${AGE_MIN}m healthy)."
          else
            echo "::notice::$CUR not promoted (healthy=$HEALTHY, age=${AGE_MIN}m). Rollback target unchanged."
          fi
          aws ssm put-parameter --name "$P/current-since" --type String --value "$(date +%s)" --overwrite >/dev/null
```

### 9.2 The rollback workflow

```yaml
name: Rollback
on:
  workflow_dispatch:
    inputs:
      tag:    { description: 'Image tag. Empty = last known good.', required: false, default: '' }
      reason: { description: 'Why?', required: false, default: 'Not specified' }
permissions: { contents: read, id-token: write }
concurrency: { group: rollback-prod, cancel-in-progress: false }   # separate from deploy

jobs:
  rollback:
    runs-on: ubuntu-latest
    steps:
      - uses: aws-actions/configure-aws-credentials@v6
        with:
          role-to-assume: ${{ vars.AWS_ROLE_ARN }}
          aws-region: ${{ vars.AWS_REGION }}

      - name: Resolve the target and roll back
        run: |
          set -euo pipefail
          P="${{ vars.SSM_PARAM_PREFIX }}"
          TAG="${{ inputs.tag }}"
          if [ -z "$TAG" ]; then
            TAG="$(aws ssm get-parameter --name "$P/last-known-good-tag" --query Parameter.Value --output text)"
          fi
          echo "Rolling back to $TAG"

          # Confirm the image EXISTS before touching production.
          aws ecr describe-images --repository-name "${{ vars.ECR_REPOSITORY }}" \
            --image-ids "imageTag=$TAG" >/dev/null \
            || { echo "::error::image $TAG is not in ECR"; exit 1; }

          CID="$(aws ssm send-command \
            --document-name "${{ vars.SSM_DOCUMENT }}" \
            --targets "Key=tag:${{ vars.INSTANCE_TAG_KEY }},Values=${{ vars.INSTANCE_TAG_VALUE }}" \
            --parameters "ImageTag=$TAG,Action=deploy" \
            --comment "ROLLBACK to $TAG by ${GITHUB_ACTOR}: ${{ inputs.reason }}" \
            --query 'Command.CommandId' --output text)"
          # ... same wait-and-stream block as the deploy workflow ...
          echo "### Rollback to \`$TAG\`" >> "$GITHUB_STEP_SUMMARY"
          echo "**No image was built during this rollback.**" >> "$GITHUB_STEP_SUMMARY"
```

**There is no `docker build` anywhere in this workflow.** That is the deliverable
for target 1 — point at it during the demo.

### 9.3 Verify Phase 4

- [ ] Deploy a visibly different version, then roll back — page reverts
- [ ] **ECR gains no new image** — this is the proof
- [ ] Run rollback twice — second run is harmless
- [ ] Rollback with an explicit older tag works
- [ ] Time it. Report the number

---

## 10. Phase 5 — Automated rollback

Two layers, because they catch different failures.

### 10.1 Pre-switch gate (already built)

The health check in §7.3 means a version that cannot start **never receives
traffic**. This is strictly better than ECS's circuit breaker, which switches
first and reverts after.

### 10.2 Post-switch alarm watch

Some failures pass a health check: the app is up, but every request 500s.

1. Create a CloudWatch alarm on 5xx rate (from the ALB if present, or a metric
   filter over the nginx/app logs).
2. Add a step at the end of the deploy workflow that watches it:

```yaml
      - name: Watch for errors after the switch
        run: |
          set -euo pipefail
          DEADLINE=$(( $(date +%s) + 300 ))     # 5 minutes
          while [ "$(date +%s)" -lt "$DEADLINE" ]; do
            STATE="$(aws cloudwatch describe-alarms --alarm-names "<APP>-5xx" \
                      --query 'MetricAlarms[0].StateValue' --output text)"
            if [ "$STATE" = "ALARM" ]; then
              echo "::error::5xx alarm fired after deploy - rolling back automatically."
              GOOD="$(aws ssm get-parameter --name "${{ vars.SSM_PARAM_PREFIX }}/last-known-good-tag" \
                       --query Parameter.Value --output text)"
              aws ssm send-command --document-name "${{ vars.SSM_DOCUMENT }}" \
                --targets "Key=tag:${{ vars.INSTANCE_TAG_KEY }},Values=${{ vars.INSTANCE_TAG_VALUE }}" \
                --parameters "ImageTag=$GOOD,Action=deploy" \
                --comment "AUTO-ROLLBACK: 5xx alarm after ${{ needs.build.outputs.tag }}"
              exit 1
            fi
            sleep 15
          done
          echo "No alarm in the watch window."
```

> Tune the alarm before wiring it to an automatic action. A too-sensitive alarm
> causes flapping rollbacks and destroys trust in the pipeline faster than any
> outage. Run it in report-only mode for a week first.

### 10.3 Verify Phase 5

- [ ] Deploy an image whose health check fails → command fails, traffic never
      switched, production untouched
- [ ] Deploy an image that is healthy but returns 500s → alarm fires → automatic
      rollback → alarm clears

---

## 11. Phase 6 — Decommission SSH

**Only after Phases 1–5 have run in production for at least a week.**

1. Remove the SSH deploy job from the workflow.
2. **Delete the SSH private key from GitHub secrets.** Target 2 is not met until
   this is gone — a key that exists is a key that can be used.
3. Remove port 22 from the instance security group (or restrict to a bastion).
4. Remove the public key from `~/.ssh/authorized_keys` on the instance.
5. Rotate: if that key was ever committed, shared, or is of unknown provenance,
   treat it as compromised and tell the security owner.
6. Document that shell access is now `aws ssm start-session --target <id>`.

- [ ] `ssh` to the instance now fails
- [ ] Session Manager still works
- [ ] A deploy still works

---

## 12. Acceptance criteria

Map every claim to evidence you can show on a screen.

| Target | Evidence |
|---|---|
| **1.** One-command rollback, no rebuild | The Rollback workflow contains no `docker build`. Run it: the app reverts, ECR gains **no** new image, the summary says so, and the elapsed time is on the run page |
| **2.** OIDC + SSM, no stored SSH key | No SSH key in GitHub secrets (screenshot the empty list). The deploy job shows `configure-aws-credentials` assuming a role. The CI role's IAM policy permits `ssm:SendCommand` **on one document only** — it cannot run arbitrary commands. Port 22 closed. `aws ssm start-session` demonstrated |
| **3.** Zero-downtime + health checks + automated rollback | Continuous `curl` across a deploy: all 200s. Deploy a broken image: command fails, traffic never switches. Deploy a 500-ing image: alarm fires, automatic rollback |

---

## 13. Risks and gotchas

| Risk | Mitigation |
|---|---|
| Instances not SSM-managed | Phase 0 §3.2. Blocks everything — resolve first |
| No IAM permission to create roles | §4. Ask early; this is the usual long pole |
| Not enough RAM for two colours | §3.4. Fall back to stop-then-start (slower rollback, still no rebuild) |
| App has no `/health` | Prerequisite work. Targets 1 and 3 depend on it |
| App does not handle `SIGTERM` | In-flight requests dropped on every deploy. Fix, or accept brief errors |
| Secrets in a `.env` on the box | Move to SSM Parameter Store (`SecureString`) or Secrets Manager. Do it as its own change |
| **Database migrations** | Rollback covers the app tier only. Schema changes must be backwards-compatible (expand/contract) or rollback breaks. Raise this explicitly — it is the first thing a DBA will ask |
| Disk fills with old images | `docker image prune`; ECR lifecycle policy — but **exclude images still referenced**, or you will delete your rollback targets |
| ECR auth expires (12h) | The script logs in on every run. Never cache credentials on the box |
| GitHub org uses immutable OIDC IDs | §4 warning. Run `Debug OIDC` first |
| Single instance = single point of failure | Out of scope, but say it. Two instances behind an ALB is the real answer |
| Change fatigue / no window | Phases 1, 4, 5 are safe any time. Only 2, 3, 6 need a window |

---

## 14. Suggested sequencing

| Week | Work | Production impact |
|---|---|---|
| 1 | Phase 0 discovery; request IAM changes | None |
| 2 | Phase 1 — ECR + OIDC + build in CI | None (SSH deploy unchanged) |
| 3 | Phase 2 — SSM document, swap deploy path | One window; SSH kept as fallback |
| 3 | Phase 3 — prove zero downtime | None |
| 4 | Phase 4 — rollback workflow + last-known-good | None (additive) |
| 5 | Phase 5 — alarm and automated rollback | None (watch-only first) |
| 6 | Phase 6 — delete the SSH key, close port 22 | One window |

Do not compress this. The value is in each phase being separately provable.

---

## 15. Reference — IAM policies

### 15.1 Trust policy for the GitHub role

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Federated": "arn:aws:iam::<ACCOUNT>:oidc-provider/token.actions.githubusercontent.com" },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": { "token.actions.githubusercontent.com:aud": "sts.amazonaws.com" },
      "StringLike":   { "token.actions.githubusercontent.com:sub": "repo:<ORG>/<REPO>:*" }
    }
  }]
}
```

Replace the `sub` with the real claim printed by `Debug OIDC` — see §4.

### 15.2 Permissions policy for the GitHub role

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "EcrAuth", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*" },
    { "Sid": "EcrThisRepoOnly", "Effect": "Allow",
      "Action": ["ecr:BatchCheckLayerAvailability","ecr:InitiateLayerUpload","ecr:UploadLayerPart",
                 "ecr:CompleteLayerUpload","ecr:PutImage","ecr:BatchGetImage",
                 "ecr:GetDownloadUrlForLayer","ecr:DescribeImages"],
      "Resource": "arn:aws:ecr:<REGION>:<ACCOUNT>:repository/<APP_NAME>" },

    { "Sid": "RunOnlyTheDeployDocument", "Effect": "Allow", "Action": "ssm:SendCommand",
      "Resource": "arn:aws:ssm:<REGION>:<ACCOUNT>:document/<App>-Deploy" },
    { "Sid": "OnlyOnTaggedInstances", "Effect": "Allow", "Action": "ssm:SendCommand",
      "Resource": "arn:aws:ec2:<REGION>:<ACCOUNT>:instance/*",
      "Condition": { "StringEquals": { "ssm:resourceTag/<INSTANCE_TAG_KEY>": "<INSTANCE_TAG_VALUE>" } } },
    { "Sid": "ReadCommandResults", "Effect": "Allow",
      "Action": ["ssm:GetCommandInvocation","ssm:ListCommandInvocations","ssm:ListCommands"],
      "Resource": "*" },

    { "Sid": "DeployState", "Effect": "Allow",
      "Action": ["ssm:GetParameter","ssm:GetParameters","ssm:PutParameter"],
      "Resource": "arn:aws:ssm:<REGION>:<ACCOUNT>:parameter/<app>/prod/*" },

    { "Sid": "ResolveTargets", "Effect": "Allow", "Action": "ec2:DescribeInstances", "Resource": "*" },
    { "Sid": "WatchAlarms", "Effect": "Allow", "Action": "cloudwatch:DescribeAlarms", "Resource": "*" }
  ]
}
```

**The two `SendCommand` statements together are the security story for target
2.** CI may run exactly one document, on exactly the instances carrying one tag.
It cannot run `AWS-RunShellScript`, cannot touch other instances, and holds no
credential that outlives the job.

### 15.3 EC2 instance profile

- `AmazonSSMManagedInstanceCore` (managed)
- ECR pull, scoped:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*" },
    { "Effect": "Allow",
      "Action": ["ecr:BatchGetImage","ecr:GetDownloadUrlForLayer","ecr:BatchCheckLayerAvailability"],
      "Resource": "arn:aws:ecr:<REGION>:<ACCOUNT>:repository/<APP_NAME>" },
    { "Effect": "Allow", "Action": ["ssm:GetParameter","ssm:GetParameters","ssm:PutParameter"],
      "Resource": "arn:aws:ssm:<REGION>:<ACCOUNT>:parameter/<app>/prod/*" }
  ]
}
```

---

## 16. What carries over from the POC

Reusable as-is, with names changed:

| From the POC | Reuse |
|---|---|
| Build job: SHA tag, smoke test, push | §6.3, nearly verbatim |
| Last-known-good promotion rule | §9.1 — same trap, same fix |
| Separate concurrency groups for deploy and rollback | §9.2 |
| `Debug OIDC` workflow | Run it before writing the trust policy (§4) |
| Structured JSON logging with a `version` field | Add to their app; makes per-release log filtering possible |
| `What is live?` workflow | Adapt to read the SSM parameters instead of ECS |
| Run summaries as the audit trail | Everywhere |

What does **not** carry over: everything ECS-specific — task definitions,
`update-service`, the deployment circuit breaker. On EC2 those roles are played
by the SSM document, the nginx upstream switch, and the pre-switch health gate.
