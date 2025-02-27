import sys
import pathlib
from pydantic import ValidationError

from contentctl.input.yml_reader import YmlReader
from contentctl.objects.baseline import Baseline
from contentctl.objects.enums import SecurityContentType
from contentctl.objects.enums import SecurityContentProduct


class BaselineBuilder():
    baseline : Baseline

    def setObject(self, path: pathlib.Path) -> None:
        yml_dict = YmlReader.load_file(path)
        yml_dict["tags"]["name"] = yml_dict["name"]
        
        try:
            self.baseline = Baseline.parse_obj(yml_dict)
            
        
        except ValidationError as e:
            print('Validation Error for file ' + str(path))
            print(e)
            sys.exit(1)

    def addDeployment(self, deployments: list) -> None:
        matched_deployments = []

        for d in deployments:
            d_tags = dict(d.tags)
            for d_tag in d_tags.keys():
                for attr in dir(self.baseline):
                    if not (attr.startswith('__') or attr.startswith('_')):
                        if attr == d_tag:
                            if type(self.baseline.__getattribute__(attr)) is str:
                                attr_values = [self.baseline.__getattribute__(attr)]
                            else:
                                attr_values = self.baseline.__getattribute__(attr)
                            
                            for attr_value in attr_values:
                                if attr_value == d_tags[d_tag]:
                                    matched_deployments.append(d)

        if len(matched_deployments) == 0:
            raise ValueError('No deployment found for baseline: ' + self.baseline.name)

        self.baseline.deployment = matched_deployments[-1]


    def reset(self) -> None:
        self.baseline = None


    def getObject(self) -> Baseline:
        return self.baseline