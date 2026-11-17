"""Session-state keys shared by tools and callbacks."""


class K:
    RESUME = "resume_text"
    RESUME_SOURCE = "resume_source"
    JD = "job_description"
    JOB_META = "job_meta"
    DATES = "employment_analysis"
    EVAL = "evaluation"
    DOCS = "documents"
    APPLICATION = "application"

    @staticmethod
    def fcot(stage: str) -> str:
        return f"fcot_{stage}"
