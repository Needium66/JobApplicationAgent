"""Binary documents (tailored resume / cover letter) in Cloud Storage, or ./local_output locally."""
import os


class Blobs:
    def __init__(self, settings):
        self.s = settings

    @property
    def cloud(self) -> bool:
        return self.s.backend == "gcp" and bool(self.s.docs_bucket)

    def put(self, path: str, data: bytes, content_type: str) -> str:
        if self.cloud:
            from google.cloud import storage
            storage.Client(project=self.s.project_id).bucket(self.s.docs_bucket).blob(path) \
                .upload_from_string(data, content_type=content_type)
            return f"gs://{self.s.docs_bucket}/{path}"
        local = os.path.join(self.s.local_output_dir, path)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "wb") as f:
            f.write(data)
        return os.path.abspath(local)

    def get(self, location: str) -> bytes:
        if location.startswith("gs://"):
            from google.cloud import storage
            bucket, _, name = location[5:].partition("/")
            return storage.Client(project=self.s.project_id or None).bucket(bucket).blob(name).download_as_bytes()
        with open(location, "rb") as f:
            return f.read()
