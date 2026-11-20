"""Helpers for HTTP API tests: signed Supabase-style tokens and an in-memory API stack."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import replace

import jwt

from command_center.api import ApiService
from command_center.auth import AuthConfig, InMemoryMembershipDirectory, Membership, TokenVerifier
from command_center.commands import CommandService
from command_center.http_api import HttpApi, HttpRequest
from command_center.jobs import InMemoryJobQueue, JobQueueRules, agent_run_reclaim_policy
from command_center.policy import OrganizationPolicy
from command_center.ratelimit import InMemoryRateLimiter
from command_center.tenancy import TenantRegistry

from .conftest import AGENT_DIR
from .helpers import contract_responder, runtime_cc

SECRET = "test-hs256-secret-with-enough-length-0123456789"
ISSUER = "https://example-project.supabase.co/auth/v1"
ALL_AGENTS = sorted(f"run_agent:{p.stem}" for p in AGENT_DIR.glob("*.yaml"))
FULL = ALL_AGENTS + ["auto_execute:GREEN", "tasks:write", "outputs:review", "recommendations:decide",
                     "changes:write", "audit:read", "performance:read", "alerts:write", "operations:read"]


def token(sub: str, *, exp_in: int = 600, secret: str = SECRET, iss: str = ISSUER, aud: str = "authenticated",
          role: str = "authenticated", alg: str = "HS256", **claims) -> str:
    now = int(time.time())
    body = {"sub": sub, "exp": now + exp_in, "iat": now, "iss": iss, "aud": aud, "role": role, **claims}
    return jwt.encode(body, secret, algorithm=alg)


class Stack:
    def __init__(self, clock, orgs=("org_a", "org_b"), *, responder=None, policy_overrides=None):
        self.tenants = TenantRegistry()
        self.ccs = {}
        for org in orgs:
            pol = replace(OrganizationPolicy.load(), organization_id=org)
            if policy_overrides:
                pol = replace(pol, **policy_overrides)
            cc, mock = runtime_cc(clock, responder=responder or contract_responder(), policy=pol)
            self.tenants.register(cc)
            self.ccs[org] = cc
        self.queue = InMemoryJobQueue(JobQueueRules(safe_to_reclaim=agent_run_reclaim_policy(self.tenants)),
                                      clock=clock)
        self.directory = InMemoryMembershipDirectory()
        self.limiter = InMemoryRateLimiter(clock)
        self.api = ApiService(self.tenants, CommandService(self.tenants, queue=self.queue))
        self.verifier = TokenVerifier(AuthConfig(issuer=ISSUER, hs256_secret=SECRET))
        self.http = HttpApi(self.api, self.verifier, self.directory, self.limiter)

    def member(self, org: str, roles=("FOUNDER", "FINANCE_APPROVER"), perms=FULL, user: str | None = None) -> str:
        user = user or str(uuid.uuid4())
        self.directory.add(Membership(org, user, frozenset(roles), frozenset(perms)))
        return user

    def call(self, method: str, path: str, user: str | None = None, body=None, query=None, headers=None, tok=None):
        h = {k.lower(): v for k, v in (headers or {}).items()}
        if tok is None and user is not None:
            tok = token(user)
        if tok is not None:
            h["authorization"] = f"Bearer {tok}"
        raw = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        resp = self.http.handle(HttpRequest(method, path, query or {}, h, raw))
        return resp.status, resp.body, resp.headers

    def drain(self):
        from command_center.jobs import AGENT_RUN, JobWorker, agent_run_handler
        return JobWorker(self.queue, {AGENT_RUN: agent_run_handler(self.tenants)}, "test-worker").drain()
