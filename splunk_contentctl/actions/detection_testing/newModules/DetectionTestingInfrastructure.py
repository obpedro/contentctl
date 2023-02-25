import threading
from pydantic import BaseModel, PrivateAttr, Field
from dataclasses import dataclass
import abc
import requests
import splunklib.client as client
from splunk_contentctl.objects.detection import Detection
from splunk_contentctl.objects.unit_test_test import UnitTestTest
from splunk_contentctl.objects.unit_test_attack_data import UnitTestAttackData
from splunk_contentctl.objects.unit_test_result import UnitTestResult
from splunk_contentctl.objects.test_config import (
    TestConfig,
    CONTAINER_APP_DIR,
    LOCAL_APP_DIR,
)
from shutil import copyfile


import configparser
from ssl import SSLEOFError
import time
import uuid
import docker
import docker.models
import docker.models.resource
import docker.models.containers
import docker.types

from tempfile import TemporaryDirectory, mktemp
import pathlib
from splunk_contentctl.helper.utils import Utils
from splunk_contentctl.actions.detection_testing.modules.DataManipulation import (
    DataManipulation,
)
import splunklib.results
from urllib3 import disable_warnings
import urllib.parse
import json
from typing import Union
import datetime
import progressbar
import tqdm


MAX_TEST_NAME_LENGTH = 70
TESTING_STATES = [
    "Downloading Data",
    "Replaying Data",
    "Waiting for Processing",
    "Running Search",
    "Deleting Data",
]

LONGEST_STATE = max(len(w) for w in TESTING_STATES)
PBAR_FORMAT_STRING = "{test_name} >> {state} | Time: {time}"


class ContainerStoppedException(Exception):
    pass


@dataclass(frozen=False)
class DetectionTestingManagerOutputDto:
    inputQueue: list[Detection] = Field(default_factory=list)
    outputQueue: list[Detection] = Field(default_factory=list)
    currentTestingQueue: dict[str, Union[Detection, None]] = Field(default_factory=dict)
    start_time: Union[datetime.datetime, None] = None
    replay_index: str = "main"
    replay_host: str = "CONTENTCTL_HOST"
    timeout_seconds: int = 120
    terminate: bool = False


class DetectionTestingInfrastructure(BaseModel, abc.ABC):
    # thread: threading.Thread = threading.Thread()
    config: TestConfig
    sync_obj: DetectionTestingManagerOutputDto
    hec_token: str = ""
    hec_channel: str = ""
    _conn: client.Service = PrivateAttr()
    pbar: tqdm.tqdm = None

    class Config:
        arbitrary_types_allowed = True

    def __init__(self, **data):
        super().__init__(**data)

    def start(self):
        raise (
            NotImplementedError(
                "start() is not implemented for Abstract Type DetectionTestingInfrastructure"
            )
        )

    def get_name(self) -> str:
        raise (
            NotImplementedError(
                "get_name() is not implemented for Abstract Type DetectionTestingInfrastructure"
            )
        )

    def setup(self):
        self.pbar = tqdm.tqdm(
            total=100,
            initial=0,
            bar_format="PLACEHOLDER",
        )
        start_time = time.time()
        try:
            for func, msg in [
                (self.start, "Starting"),
                (self.get_conn, "Getting API Connection"),
                (self.configure_imported_roles, "Configuring Roles"),
                (self.configure_delete_indexes, "Configuring Indexes"),
                (self.configure_conf_file_datamodels, "Configuring Datamodels"),
                (self.configure_hec, "Configuring HEC"),
                (self.wait_for_ui_ready, "Waiting for UI"),
            ]:
                self.pbar.bar_format = self.format_pbar_string(
                    self.get_name(), msg, start_time
                )
                self.pbar.update()
                func()
                self.check_for_teardown()

        except Exception as e:
            print(e)
            self.finish()
            return

        self.pbar.bar_format = self.format_pbar_string(
            self.get_name(), "Finished Setup!", start_time
        )
        self.pbar.update()

    def wait_for_ui_ready(self):
        # print("waiting for ui...")
        self.get_conn()
        # print("done waiting for ui")

    def configure_hec(self):
        self.hec_channel = str(uuid.uuid4())
        try:
            res = self.get_conn().input(
                path="/servicesNS/nobody/splunk_httpinput/data/inputs/http/http:%2F%2FDETECTION_TESTING_HEC"
            )
            self.hec_token = str(res.token)
            print(
                f"HEC Endpoint for [{self.get_name()}] already exists with token [{self.hec_token}].  Using channel [{self.hec_channel}]"
            )
            return
        except Exception as e:
            # HEC input does not exist.  That's okay, we will create it
            pass

        try:

            res = self.get_conn().inputs.create(
                name="DETECTION_TESTING_HEC",
                kind="http",
                index="main",
                indexes="main,_internal,_audit",
                useACK=True,
            )
            self.hec_token = str(res.token)
            # print(
            #     f"Successfully configured HEC Endpoint for [{self.get_name()}] with token [{self.hec_token}] and channel [{self.hec_channel}]"
            # )
            return

        except Exception as e:
            raise (Exception(f"Failure creating HEC Endpoint: {str(e)}"))

    def get_conn(self) -> client.Service:
        try:
            if not self._conn:
                self.connect_to_api()
            elif self._conn.restart_required:
                # continue trying to re-establish a connection until after
                # the server has restarted
                self.connect_to_api()
        except Exception as e:
            # there was some issue getting the connection. Try again just once
            self.connect_to_api()
        return self._conn

    def check_for_teardown(self):
        # Make sure we can easily quit during setup if we need to.
        # Some of these stages can take a long time
        if self.sync_obj.terminate:
            # Exiting in a thread just quits the thread, not the entire process
            raise (ContainerStoppedException(f"Testing stopped for {self.get_name()}"))

    def connect_to_api(self):
        while True:
            self.check_for_teardown()
            time.sleep(5)
            try:

                conn = client.connect(
                    host=self.config.test_instance_address,
                    port=self.config.api_port,
                    username=self.config.splunk_app_username,
                    password=self.config.splunk_app_password,
                )

                if conn.restart_required:
                    # we will wait and try again
                    # print("there is a pending restart")
                    continue
                # Finished setup
                self._conn = conn
                return
            except ConnectionRefusedError as e:
                raise (e)
            except SSLEOFError as e:
                pass
                # print(
                #     "Waiting to connect to Splunk Infrastructure for Configuration..."
                # )
            except Exception as e:
                print(
                    f"Unhandled exception getting connection to splunk server: {str(e)}"
                )
                self.sync_obj.terminate = True

    def configure_imported_roles(
        self, imported_roles: list[str] = ["user", "power", "can_delete"]
    ):

        self.get_conn().roles.post(
            self.config.splunk_app_username, imported_roles=imported_roles
        )

    def configure_delete_indexes(self, indexes: list[str] = ["_*", "*", "main"]):
        endpoint = "/services/properties/authorize/default/deleteIndexesAllowed"
        indexes_encoded = ";".join(indexes)
        try:
            self.get_conn().post(endpoint, value=indexes_encoded)
        except Exception as e:
            print(
                f"Error configuring deleteIndexesAllowed with '{indexes_encoded}': [{str(e)}]"
            )

    def wait_for_conf_file(self, app_name: str, conf_file_name: str):
        while True:
            self.check_for_teardown()
            time.sleep(1)
            try:
                res = self.get_conn().get(
                    f"configs/conf-{conf_file_name}", app=app_name
                )
                # print(f"configs/conf-{conf_file_name} exists")
                return
            except Exception as e:
                pass
                # print(f"Waiting for [{app_name} - {conf_file_name}.conf: {str(e)}")

    def configure_conf_file_datamodels(self, APP_NAME: str = "Splunk_SA_CIM"):
        self.wait_for_conf_file(APP_NAME, "datamodels")

        parser = configparser.ConfigParser()
        parser.read("/tmp/datamodels.conf")

        for datamodel_name in parser:
            if datamodel_name == "DEFAULT":
                # Skip the DEFAULT section for configparser
                continue
            for name, value in parser[datamodel_name].items():
                try:
                    res = self.get_conn().post(
                        f"properties/datamodels/{datamodel_name}/{name}",
                        app=APP_NAME,
                        value=value,
                    )

                except Exception as e:
                    print(
                        f"Error creating the conf Datamodel {datamodel_name} key/value {name}/{value}: {str(e)}"
                    )

    def execute(self):
        while True:
            try:
                self.check_for_teardown()
            except ContainerStoppedException as e:
                print(f"Stopped container [{self.get_name()}]")
                return

            try:
                detection = self.sync_obj.inputQueue.pop()
                self.sync_obj.currentTestingQueue[self.get_name()] = detection
            except IndexError as e:
                print(f"No more detections to test, shutting down {self.get_name()}")
                self.finish()
                return
            try:
                self.test_detection(detection)
            except ContainerStoppedException as e:
                print(f"Stopped container [{self.get_name()}]")
                return
            except Exception as e:
                print(f"Error testing detection: {str(e)}")
            finally:
                self.sync_obj.outputQueue.append(detection)
                self.sync_obj.currentTestingQueue[self.get_name()] = None

    def test_detection(self, detection: Detection):
        if detection.test is None:
            print(f"No test(s) found for {detection.name}")
            return

        for test in detection.test.tests:
            self.execute_test(detection, test)
            """
            if test.result is not None and test.result.success:
                print(
                    f"TEST RESULT: [{test.name}] - PASS ({test.result.duration} seconds)"
                )
            elif test.result is not None:
                print(
                    f"TEST RESULT: [{test.name}] - FAIL ({test.result.duration} seconds)"
                )
            else:
                print(f"TEST RESULT: [{test.name}] - FAIL (UNKOWN TIME)")
            """

    def format_pbar_string(self, test_name: str, state: str, start_time: float) -> str:
        test_name = test_name.ljust(MAX_TEST_NAME_LENGTH)
        state = state.ljust(LONGEST_STATE)
        runtime = datetime.timedelta(seconds=round(time.time() - start_time))
        return PBAR_FORMAT_STRING.format(test_name=test_name, state=state, time=runtime)

    def execute_test(
        self, detection: Detection, test: UnitTestTest, FORCE_ALL_TIME: bool = True
    ):

        self.pbar.reset()

        start_time = time.time()

        # https://github.com/WoLpH/python-progressbar/issues/164
        # Use NullBar if there is more than 1 container or we are running
        # in a non-interactive context

        try:
            self.replay_attack_data_files(test.attack_data, test, start_time)
        except Exception as e:
            test.result = UnitTestResult()
            test.result.set_job_content(
                e, self.config, duration=time.time() - start_time
            )
            self.pbar.write(
                self.format_pbar_string(
                    test.name,
                    "\x1b[0;30;41m" + "FAIL".ljust(LONGEST_STATE) + "\x1b[0m",
                    start_time,
                )
            )
            return

        # Set the mode and timeframe, if required
        kwargs = {"exec_mode": "blocking"}
        if not FORCE_ALL_TIME:
            if test.earliest_time is not None:
                kwargs.update({"earliest_time": test.earliest_time})
            if test.latest_time is not None:
                kwargs.update({"latest_time": test.latest_time})

        try:
            self.retry_search_until_timeout(detection, test, kwargs, start_time)
        except ContainerStoppedException as e:
            raise (e)
        except Exception as e:
            test.result = UnitTestResult()
            test.result.set_job_content(
                e, self.config, duration=time.time() - start_time
            )

        self.pbar.bar_format = self.format_pbar_string(
            test.name, "Deleting Data", start_time
        )
        self.pbar.update()
        self.delete_attack_data(test.attack_data)

        if test.result is not None and test.result.success:
            self.pbar.write(
                self.format_pbar_string(
                    test.name,
                    "\x1b[0;30;42m" + "PASS".ljust(LONGEST_STATE) + "\x1b[0m",
                    start_time,
                )
            )

        else:
            self.pbar.write(
                self.format_pbar_string(
                    test.name,
                    "\x1b[0;30;41m" + "FAIL".ljust(LONGEST_STATE) + "\x1b[0m",
                    start_time,
                )
            )

        if test.result is not None:
            test.result.duration = round(time.time() - start_time, 2)

    def retry_search_until_timeout(
        self,
        detection: Detection,
        test: UnitTestTest,
        kwargs: dict,
        start_time: float,
    ):
        search_start_time = time.time()
        search_stop_time = time.time() + self.sync_obj.timeout_seconds

        if test.pass_condition is None:
            # we will default to ensuring at least one result exists
            search = detection.search
        else:
            search = f"{detection.search} {test.pass_condition}"

        # Search that do not begin with '|' must begin with 'search '
        if not search.strip().startswith("|"):
            if not search.strip().startswith("search "):
                search = f"search {search}"

        # exponential backoff for wait time
        tick = 2

        while time.time() < search_stop_time:

            for _ in range(pow(2, tick - 1)):
                # This loop allows us to capture shutdown events without being
                # stuck in an extended sleep. Remember that this raises an exception
                self.check_for_teardown()
                self.pbar.bar_format = self.format_pbar_string(
                    test.name, "Waiting for Processing", start_time
                )
                self.pbar.update()

                time.sleep(1)

            self.pbar.bar_format = self.format_pbar_string(
                test.name, "Running Search", start_time
            )
            self.pbar.update()

            job = self.get_conn().search(query=search, **kwargs)

            # the following raises an error if there is an exception in the search
            _ = job.results(output_mode="json")

            if int(job.content.get("resultCount", "0")) > 0:
                test.result = UnitTestResult()
                test.result.set_job_content(
                    job.content,
                    self.config,
                    success=True,
                    duration=time.time() - search_start_time,
                )

                return
            else:
                test.result = UnitTestResult()
                test.result.set_job_content(
                    job.content,
                    self.config,
                    success=False,
                    duration=time.time() - search_start_time,
                )

            tick += 1

        return

    def delete_attack_data(self, attack_data_files: list[UnitTestAttackData]):
        for attack_data_file in attack_data_files:
            index = attack_data_file.custom_index or self.sync_obj.replay_index
            host = attack_data_file.host or self.sync_obj.replay_host
            splunk_search = f'search index="{index}" host="{host}" | delete'
            kwargs = {"exec_mode": "blocking"}
            try:

                job = self.get_conn().jobs.create(splunk_search, **kwargs)
                results_stream = job.results(output_mode="json")
                reader = splunklib.results.JSONResultsReader(results_stream)

            except Exception as e:
                raise (
                    Exception(
                        f"Trouble deleting data using the search {splunk_search}: {str(e)}"
                    )
                )

    def replay_attack_data_files(
        self,
        attack_data_files: list[UnitTestAttackData],
        test: UnitTestTest,
        start_time: float,
    ):
        with TemporaryDirectory(prefix="contentctl_attack_data") as attack_data_dir:
            for attack_data_file in attack_data_files:
                self.replay_attack_data_file(
                    attack_data_file, attack_data_dir, test, start_time
                )

    def replay_attack_data_file(
        self,
        attack_data_file: UnitTestAttackData,
        tmp_dir: str,
        test: UnitTestTest,
        start_time: float,
    ):
        tempfile = mktemp(dir=tmp_dir)

        if not (
            attack_data_file.data.startswith("https://")
            or attack_data_file.data.startswith("http://")
        ):
            if pathlib.Path(attack_data_file.data).is_file():
                try:
                    copyfile(attack_data_file.data, tempfile)
                except Exception as e:
                    raise (
                        Exception(
                            f"Error copying local Attack Data File for [{Detection.name}] - [{attack_data_file.data}]: {str(e)}"
                        )
                    )
            else:
                raise (
                    Exception(
                        f"Attack Data File for [{Detection.name}] is local [{attack_data_file.data}], but does not exist."
                    )
                )

        else:
            # Download the file
            # We need to overwrite the file - mkstemp will create an empty file with the
            # given name
            try:
                # In case the path is a local file, try to get it
                self.pbar.bar_format = self.format_pbar_string(
                    test.name, "Downloading Data", start_time
                )
                self.pbar.update()
                Utils.download_file_from_http(
                    attack_data_file.data, tempfile, overwrite_file=True
                )
            except Exception as e:
                raise (
                    Exception(
                        f"Attack Data File for [{Detection.name}] is remote [{attack_data_file.data}], but could not download: :{str(e)}"
                    )
                )

        # Update timestamps before replay
        if attack_data_file.update_timestamp:
            data_manipulation = DataManipulation()
            data_manipulation.manipulate_timestamp(
                tempfile, attack_data_file.sourcetype, attack_data_file.source
            )

        # Upload the data
        self.pbar.bar_format = self.format_pbar_string(
            test.name, "Replaying Data", start_time
        )
        self.pbar.update()
        self.hec_raw_replay(tempfile, attack_data_file)

        return attack_data_file.custom_index or self.sync_obj.replay_index

    def hec_raw_replay(
        self,
        tempfile: str,
        attack_data_file: UnitTestAttackData,
        verify_ssl: bool = False,
    ):
        if verify_ssl is False:
            # need this, otherwise every request made with the requests module
            # and verify=False will print an error to the command line
            disable_warnings()

        # build the headers

        headers = {
            "Authorization": f"Splunk {self.hec_token}",  # token must begin with 'Splunk '
            "X-Splunk-Request-Channel": self.hec_channel,
        }

        url_params = {
            "index": attack_data_file.custom_index or self.sync_obj.replay_index,
            "source": attack_data_file.source,
            "sourcetype": attack_data_file.sourcetype,
            "host": attack_data_file.host or self.sync_obj.replay_host,
        }

        if self.config.test_instance_address.strip().lower().startswith("https://"):
            address_with_scheme = self.config.test_instance_address.strip().lower()
        elif self.config.test_instance_address.strip().lower().startswith("http://"):
            address_with_scheme = (
                self.config.test_instance_address.strip()
                .lower()
                .replace("http://", "https://")
            )
        else:
            address_with_scheme = f"https://{self.config.test_instance_address}"

        # Generate the full URL, including the host, the path, and the params.
        # We can be a lot smarter about this (and pulling the port from the url, checking
        # for trailing /, etc, but we leave that for the future)
        url_with_port = f"{address_with_scheme}:{self.config.hec_port}"
        url_with_hec_path = urllib.parse.urljoin(
            url_with_port, "services/collector/raw"
        )
        with open(tempfile, "rb") as datafile:
            try:
                res = requests.post(
                    url_with_hec_path,
                    params=url_params,
                    data=datafile.read(),
                    allow_redirects=True,
                    headers=headers,
                    verify=verify_ssl,
                )
                jsonResponse = json.loads(res.text)

            except Exception as e:
                raise (
                    Exception(
                        f"There was an exception sending attack_data to HEC: {str(e)}"
                    )
                )

        if "ackId" not in jsonResponse:
            raise (
                Exception(
                    f"key 'ackID' not present in response from HEC server: {jsonResponse}"
                )
            )

        ackId = jsonResponse["ackId"]
        url_with_hec_ack_path = urllib.parse.urljoin(
            url_with_port, "services/collector/ack"
        )

        requested_acks = {"acks": [jsonResponse["ackId"]]}
        while True:
            try:

                res = requests.post(
                    url_with_hec_ack_path,
                    json=requested_acks,
                    allow_redirects=True,
                    headers=headers,
                    verify=verify_ssl,
                )

                jsonResponse = json.loads(res.text)

                if "acks" in jsonResponse and str(ackId) in jsonResponse["acks"]:
                    if jsonResponse["acks"][str(ackId)] is True:
                        # ackID has been found for our request, we can return as the data has been replayed
                        return
                    else:
                        # ackID is not yet true, we will wait some more
                        time.sleep(2)

                else:
                    raise (
                        Exception(
                            f"Proper ackID structure not found for ackID {ackId} in {jsonResponse}"
                        )
                    )
            except Exception as e:
                raise (Exception(f"There was an exception in the post: {str(e)}"))

    def status(self):
        pass

    def finish(self):
        self.pbar.close()

    def check_health(self):
        pass


class DetectionTestingContainer(DetectionTestingInfrastructure):
    container: docker.models.resource.Model = None

    def start(self):
        self.container = self.make_container()
        self.container.start()

    def finish(self):
        if self.container is not None:
            try:
                self.removeContainer()
            except Exception as e:
                raise (Exception(f"Error removing container: {str(e)}"))

    def get_name(self) -> str:
        return self.config.container_name

    def get_docker_client(self):
        try:
            c = docker.client.from_env()

            return c
        except Exception as e:
            raise (Exception(f"Failed to get docker client: {str(e)}"))

    def check_for_teardown(self):

        try:
            self.get_docker_client().containers.get(self.get_name())
        except Exception as e:
            if self.sync_obj.terminate is not True:
                print(f"Error: could not get container [{self.get_name()}]: {str(e)}")
                self.sync_obj.terminate = True

        if self.sync_obj.terminate:
            self.removeContainer()

        super().check_for_teardown()

    def make_container(self) -> docker.models.resource.Model:
        # First, make sure that the container has been removed if it already existed
        self.removeContainer()

        ports_dict = {
            "8000/tcp": self.config.web_ui_port,
            "8088/tcp": self.config.hec_port,
            "8089/tcp": self.config.api_port,
        }

        mounts = [
            docker.types.Mount(
                source=str(LOCAL_APP_DIR.absolute()),
                target=str(CONTAINER_APP_DIR.absolute()),
                type="bind",
                read_only=True,
            )
        ]

        environment = {}
        environment["SPLUNK_START_ARGS"] = "--accept-license"
        environment["SPLUNK_PASSWORD"] = self.config.splunk_app_password
        environment["SPLUNK_APPS_URL"] = ",".join(
            p.environment_path for p in self.config.apps
        )
        if (
            self.config.splunkbase_password is not None
            and self.config.splunkbase_username is not None
        ):
            environment["SPLUNKBASE_USERNAME"] = self.config.splunkbase_username
            environment["SPLUNKBASE_PASSWORD"] = self.config.splunkbase_password

        container = self.get_docker_client().containers.create(
            self.config.full_image_path,
            ports=ports_dict,
            environment=environment,
            name=self.get_name(),
            mounts=mounts,
            detach=True,
        )

        return container

    def removeContainer(self, removeVolumes: bool = True, forceRemove: bool = True):

        try:
            container: docker.models.containers.Container = (
                self.get_docker_client().containers.get(self.get_name())
            )
        except Exception as e:
            # Container does not exist, no need to try and remove it
            return
        try:

            # container was found, so now we try to remove it
            # v also removes volumes linked to the container
            container.remove(v=removeVolumes, force=forceRemove)
            # remove it even if it is running. remove volumes as well
            # No need to print that the container has been removed, it is expected behavior

        except Exception as e:
            raise (
                Exception(
                    f"Could not remove Docker Container [{self.config.container_name}]: {str(e)}"
                )
            )
