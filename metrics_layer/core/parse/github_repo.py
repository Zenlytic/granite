import os
import shutil
from glob import glob
import yaml

import git

from metrics_layer.core import utils

BASE_PATH = os.path.dirname(__file__)


class BaseRepo:
    def get_repo_type(self):
        if self.repo_type:
            return self.repo_type

        project_files = list(self.search(pattern="zenlytic_project.yaml", folders=[self.folder]))
        project_files += list(self.search(pattern="zenlytic_project.yml", folders=[self.folder]))
        if len(project_files) == 1:
            return self.read_yaml_file(project_files[0]).get("mode", "metrics_layer")
        return "metrics_layer"

    def delete(self):
        raise NotImplementedError()

    def search(self, pattern: str, folders: list = []):
        """Example arg: pattern='*.yml'"""
        return [fn for f in folders for fn in self.glob_search(f, pattern) if "venv" not in fn]

    def fetch(self):
        raise NotImplementedError()

    def get_dbt_path(self):
        pattern = "dbt_project.yml"
        in_root = list(glob(f"{self.folder}/{pattern}"))
        if len(in_root) == 1:
            return self.folder
        in_one_folder_deep = list(glob(f"{self.folder}/*/{pattern}"))
        if len(in_one_folder_deep) == 1:
            return os.path.dirname(in_one_folder_deep[0]) + "/"
        return self.folder

    @staticmethod
    def read_yaml_file(path: str):
        with open(path, "r") as f:
            yaml_dict = yaml.safe_load(f)
        return yaml_dict

    @staticmethod
    def glob_search(folder: str, pattern: str):
        return glob(f"{folder}**/{pattern}", recursive=True)


class LocalRepo(BaseRepo):
    def __init__(self, repo_path: str, repo_type: str = None) -> None:
        self.repo_path = repo_path
        self.repo_type = repo_type
        self.folder = f"{os.path.join(os.getcwd(), self.repo_path)}/"
        self.dbt_path = self.get_dbt_path()
        self.branch_options = []

    def fetch(self, private_key: str = None):
        pass

    def delete(self):
        pass


class GithubRepo(BaseRepo):
    def __init__(self, repo_url: str, branch: str, repo_type: str = None, private_key=None) -> None:
        self.repo_url = repo_url
        self.is_ssh = repo_url.startswith("git@")
        self.repo_type = repo_type
        self.repo_name = utils.generate_uuid()
        self.repo_destination = os.path.join(BASE_PATH, self.repo_name)
        self.folder = f"{self.repo_destination}/"
        self.dbt_path = None
        self.branch = branch
        self.branch_options = []

    def fetch(self, private_key: str = None):
        _, branch_options = self.fetch_github_repo(private_key)
        self.dbt_path = self.get_dbt_path()
        self.branch_options = branch_options

    def delete(self, folder: str = None):
        if folder is None:
            folder = self.folder

        if os.path.exists(folder) and os.path.isdir(folder):
            shutil.rmtree(folder)

    def fetch_github_repo(self, private_key: str):
        if self.is_ssh:
            if private_key is None:
                raise ValueError("Private key is required for SSH mode of connection to Github.")
            repo, branch_options = GithubRepo._fetch_github_repo_ssh(
                self.repo_url, self.repo_destination, self.branch, private_key
            )
        else:
            repo, branch_options = GithubRepo._fetch_github_repo_https(
                self.repo_url, self.repo_destination, self.branch
            )
        return repo, branch_options

    @staticmethod
    def _fetch_github_repo_https(repo_url: str, repo_destination: str, branch: str):
        if os.path.exists(repo_destination) and os.path.isdir(repo_destination):
            shutil.rmtree(repo_destination)
        repo = git.Repo.clone_from(repo_url, to_path=repo_destination, branch=branch, depth=1)
        branch_options = GithubRepo._fetch_branch_options(repo, branch)
        return repo, branch_options

    @staticmethod
    def _fetch_github_repo_ssh(repo_url: str, repo_destination: str, branch: str, private_key: str):
        file_name = "ssh_p8_key"
        file_path = os.path.join(BASE_PATH, file_name)
        with open(file_path, "wb") as f:
            f.write(private_key)
            try:
                os.chmod(file_path, 0o600)
            except Exception as e:
                print(f"Exception chmoding private key file: {e}")

        ssh_cmd = f"ssh -o StrictHostKeyChecking=no -i {file_path}"

        try:
            repo = git.Repo.clone_from(
                url=repo_url, to_path=repo_destination, branch=branch, env={"GIT_SSH_COMMAND": ssh_cmd}
            )
            branch_options = GithubRepo._fetch_branch_options(repo, branch)
            os.remove(file_path)
        except Exception as e:
            os.remove(file_path)
            raise e

        return repo, branch_options

    @staticmethod
    def _fetch_branch_options(repo, branch):
        try:
            raw = repo.git.ls_remote()
            branch_options = GithubRepo._parse_ls_remote(raw)
        except Exception as e:
            print(f"Exception getting branch options: {e}")
            branch_options = [branch]
        return branch_options

    @staticmethod
    def _parse_ls_remote(ls_remote_response: str):
        dynamic_branch_options = []
        for raw_branch_ref in ls_remote_response.split("\n"):
            if "/heads/" in raw_branch_ref:
                clean_branch_ref = raw_branch_ref.split("/heads/")[-1]
                dynamic_branch_options.append(clean_branch_ref)
        return dynamic_branch_options
