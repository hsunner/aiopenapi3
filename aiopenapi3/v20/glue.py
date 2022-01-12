from typing import Dict, List
import json

import httpx
import pydantic

from ..base import SchemaBase, ParameterBase
from ..request import RequestBase, AsyncRequestBase

from . import SecurityRequirement


class Request(RequestBase):
    @property
    def security(self):
        return self.api._security

    @property
    def data(self) -> SchemaBase:
        for i in filter(lambda x: x.in_ == "body", self.operation.parameters):
            return i.schema_
        raise ValueError("body")

    @property
    def parameters(self) -> Dict[str, ParameterBase]:
        return list(
            filter(lambda x: x.in_ != "body", self.operation.parameters + self.root.paths[self.path].parameters)
        )

    def args(self, content_type: str = "application/json"):
        op = self.operation
        parameters = op.parameters + self.root.paths[self.path].parameters
        schema = op.requestBody.content[content_type].schema_
        return {"parameters": parameters, "data": schema}

    def return_value(self, http_status: int = 200, content_type: str = "application/json") -> SchemaBase:
        return self.operation.responses[str(http_status)].content[content_type].schema_

    def _prepare_security(self):
        if self.operation.security == []:
            security = []
        else:
            security = (self.operation.security or []) + self.root.security

        if self.security and security:
            for scheme, value in self.security.items():
                for r in filter(lambda x: x.name == scheme, security):
                    self._prepare_secschemes(r, value)
                    break
                else:
                    continue
                break
            else:
                raise ValueError(
                    f"No security requirement satisfied (accepts {', '.join(self.operation.security.keys())})"
                )

    def _prepare_secschemes(self, security_requirement: SecurityRequirement, value: List[str]):
        """
        https://swagger.io/specification/v2/#security-scheme-object
        """
        ss = self.root.securityDefinitions[security_requirement.name]

        if ss.type == "basic":
            self.req.auth = value

        if ss.type == "apiKey":
            if ss.in_ == "query":
                # apiKey in query parameter
                self.req.params[ss.name] = value

            if ss.in_ == "header":
                # apiKey in query header data
                self.req.headers[ss.name] = value

            if ss.in_ == "cookie":
                self.req.cookies = {ss.name: value}

    def _prepare_parameters(self, parameters):
        # Parameters
        path_parameters = {}
        accepted_parameters = {}
        p = filter(lambda x: x.in_ != "body", self.operation.parameters + self.root.paths[self.path].parameters)

        for _ in list(p):
            # TODO - make this work with $refs - can operations be $refs?
            accepted_parameters.update({_.name: _})

        for name, spec in accepted_parameters.items():
            if parameters is None or name not in parameters:
                if spec.required:
                    raise ValueError(f"Required parameter {name} not provided")
                continue

            value = parameters[name]

            if spec.in_ == "path":
                # The string method `format` is incapable of partial updates,
                # as such we need to collect all the path parameters before
                # applying them to the format string.
                path_parameters[name] = value

            if spec.in_ == "query":
                self.req.params[name] = value

            if spec.in_ == "header":
                self.req.headers[name] = value

            if spec.in_ == "cookie":
                self.req.cookies[name] = value

        self.req.url = self.req.url.format(**path_parameters)

    def _prepare_body(self, data):
        try:
            self.data
        except ValueError:
            return

        if data is None and self.data.required:
            raise ValueError("Request Body is required but none was provided.")

        if "application/json" in self.operation.consumes:
            if isinstance(data, (dict, list)):
                pass
            elif isinstance(data, pydantic.BaseModel):
                data = data.dict()
            else:
                raise TypeError(data)
            data = self.api.plugins.message.marshalled(
                operationId=self.operation.operationId, marshalled=data
            ).marshalled
            data = json.dumps(data)
            data = data.encode()
            data = self.api.plugins.message.sending(operationId=self.operation.operationId, sending=data).sending
            self.req.content = data
            self.req.headers["Content-Type"] = "application/json"
        else:
            raise NotImplementedError()

    def _prepare(self, data, parameters):
        self._prepare_security()
        self._prepare_parameters(parameters)
        self._prepare_body(data)

    def _build_req(self, session):
        req = session.build_request(
            self.method,
            str(self.api.url / self.req.url[1:]),
            headers=self.req.headers,
            cookies=self.req.cookies,
            params=self.req.params,
            content=self.req.content,
        )
        return req

    def _process(self, result):
        # spec enforces these are strings
        status_code = str(result.status_code)

        # find the response model in spec we received
        expected_response = None
        if status_code in self.operation.responses:
            expected_response = self.operation.responses[status_code]
        elif "default" in self.operation.responses:
            expected_response = self.operation.responses["default"]

        if expected_response is None:
            # TODO - custom exception class that has the response object in it
            options = ",".join(self.operation.responses.keys())
            raise ValueError(
                f"""Unexpected response {result.status_code} from {self.operation.operationId} (expected one of {options}), no default is defined"""
            )

        if status_code == "204":
            return

        content_type = result.headers.get("Content-Type", None)

        if content_type.lower().partition(";")[0] == "application/json":
            data = result.text
            data = self.api.plugins.message.received(operationId=self.operation.operationId, received=data).received
            data = json.loads(data)
            data = self.api.plugins.message.parsed(operationId=self.operation.operationId, parsed=data).parsed
            data = expected_response.schema_.model(data)
            data = self.api.plugins.message.unmarshalled(
                operationId=self.operation.operationId, unmarshalled=data
            ).unmarshalled
            return data
        else:
            raise NotImplementedError(content_type)


class AsyncRequest(Request, AsyncRequestBase):
    pass