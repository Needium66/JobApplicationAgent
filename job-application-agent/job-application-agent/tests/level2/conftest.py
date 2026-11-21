import asyncio
import os
import tempfile

import pytest

os.environ["JOB_AGENT_LOCAL_ONLY"] = "true"

from fastapi.testclient import TestClient  # noqa: E402

from jobpilot.agents.application import all_specialists  # noqa: E402
from jobpilot.core.runtime import Runtime, set_runtime  # noqa: E402
from jobpilot.services.edge import create_edge_app  # noqa: E402
from jobpilot.settings import Settings  # noqa: E402
from jobpilot.testing.scripted_llm import ScriptedLlm  # noqa: E402

RESUME = ("Jane Doe — Cloud Engineer, Dallas TX. Skills: Python, GCP, Terraform, Kubernetes, CI/CD. "
          "Cloud Engineer, Acme, 2019-01 to present. DevOps Engineer, Beta, 2016-03 to 2018-12. "
          "Certifications: Google Professional Cloud Architect. Education: BSc Computer Science. ") * 2
JD = ("Senior Cloud Engineer at Contoso (Remote, US). Requirements: 5+ years cloud engineering, GCP, "
      "Terraform, Kubernetes, Python. Preferred: Professional Cloud Architect certification. ") * 2


class Env:
    def __init__(self, **llm_kwargs):
        s = Settings(backend="local", auth_mode="dev", project_id="", retry_base_s=0.001,
                     local_output_dir=tempfile.mkdtemp(), max_agent_attempts=5, rate_limit_per_min=1000)
        self.rt = set_runtime(Runtime(s))
        self.llm_kwargs = llm_kwargs
        self.models: dict[str, ScriptedLlm] = {}

        def override(name):
            kw = dict(self.llm_kwargs.get(name, {}))
            m = ScriptedLlm(agent_name=name, **kw)
            self.models.setdefault(name, m)
            return m

        self.rt.model_override = override
        for sp in all_specialists(self.rt):
            self.rt.bus.subscribe(sp.name, sp.handle, sp.accepts)
        self.client = TestClient(create_edge_app(self.rt))

    def drain(self):
        asyncio.run(self.rt.bus.drain())

    def create(self, user="ola@example.com", **body):
        payload = {"resume_text": RESUME, "job_description": JD, **body}
        return self.client.post("/api/cases", json=payload, headers={"x-dev-user": user})

    def get(self, case_id, user="ola@example.com"):
        return self.client.get(f"/api/cases/{case_id}", headers={"x-dev-user": user})

    def approve(self, case_id, user="ola@example.com", **body):
        return self.client.post(f"/api/cases/{case_id}/approve", json={"method": "portal", **body},
                                headers={"x-dev-user": user})

    def nodes(self, case_id):
        return self.rt.graph.nodes(case_id)


@pytest.fixture
def env():
    return Env()
