import re

import sentry_sdk
from rest_framework.exceptions import ParseError
from rest_framework.request import Request
from rest_framework.response import Response

from sentry import search
from sentry.api.api_publish_status import ApiPublishStatus
from sentry.api.base import EnvironmentMixin, region_silo_endpoint
from sentry.api.bases import NoProjects, OrganizationEventsEndpointBase
from sentry.api.event_search import parse_search_query
from sentry.api.helpers.group_index import build_query_params_from_request
from sentry.api.serializers import serialize
from sentry.api.serializers.models.group import GroupSerializer
from sentry.api.utils import handle_query_errors
from sentry.middleware import is_frontend_request
from sentry.snuba import spans_indexed, spans_metrics
from sentry.snuba.query_sources import QuerySource
from sentry.snuba.referrer import Referrer


@region_silo_endpoint
class OrganizationEventsMetaEndpoint(OrganizationEventsEndpointBase):
    publish_status = {
        "GET": ApiPublishStatus.PRIVATE,
    }
    snuba_methods = ["GET"]

    def get(self, request: Request, organization) -> Response:
        try:
            snuba_params, _ = self.get_snuba_dataclass(request, organization)
        except NoProjects:
            return Response({"count": 0})

        dataset = self.get_dataset(request)

        with handle_query_errors():
            result = dataset.query(
                selected_columns=["count()"],
                params={},
                snuba_params=snuba_params,
                query=request.query_params.get("query"),
                referrer=Referrer.API_ORGANIZATION_EVENTS_META.value,
                # TODO: @athena - add query_source when all datasets support it
                # query_source=(
                #     QuerySource.FRONTEND if is_frontend_request(request) else QuerySource.API
                # ),
            )

        return Response({"count": result["data"][0]["count"]})


UNESCAPED_QUOTE_RE = re.compile('(?<!\\\\)"')


@region_silo_endpoint
class OrganizationEventsRelatedIssuesEndpoint(OrganizationEventsEndpointBase, EnvironmentMixin):
    publish_status = {
        "GET": ApiPublishStatus.PRIVATE,
    }

    def get(self, request: Request, organization) -> Response:
        try:
            # events-meta is still used by events v1 which doesn't require global views
            snuba_params, _ = self.get_snuba_dataclass(
                request, organization, check_global_views=False
            )
        except NoProjects:
            return Response([])

        with sentry_sdk.start_span(op="discover.endpoint", description="find_lookup_keys") as span:
            possible_keys = ["transaction"]
            lookup_keys = {key: request.query_params.get(key) for key in possible_keys}

            if not any(lookup_keys.values()):
                return Response(
                    {
                        "detail": f"Must provide one of {possible_keys} in order to find related events"
                    },
                    status=400,
                )

        with handle_query_errors():
            with sentry_sdk.start_span(op="discover.endpoint", description="filter_creation"):
                projects = self.get_projects(request, organization)
                query_kwargs = build_query_params_from_request(
                    request, organization, projects, snuba_params.environments
                )
                query_kwargs["limit"] = 5
                try:
                    # Need to escape quotes in case some "joker" has a transaction with quotes
                    transaction_name = UNESCAPED_QUOTE_RE.sub('\\"', lookup_keys["transaction"])
                    parsed_terms = parse_search_query(f'transaction:"{transaction_name}"')
                except ParseError:
                    return Response({"detail": "Invalid transaction search"}, status=400)

                if query_kwargs.get("search_filters"):
                    query_kwargs["search_filters"].extend(parsed_terms)
                else:
                    query_kwargs["search_filters"] = parsed_terms

                query_kwargs["actor"] = request.user

            with sentry_sdk.start_span(op="discover.endpoint", description="issue_search"):
                results_cursor = search.backend.query(**query_kwargs)

        with sentry_sdk.start_span(op="discover.endpoint", description="serialize_results") as span:
            results = list(results_cursor)
            span.set_data("result_length", len(results))
            context = serialize(
                results,
                request.user,
                GroupSerializer(
                    environment_func=self._get_environment_func(request, organization.id)
                ),
            )

        return Response(context)


@region_silo_endpoint
class OrganizationSpansSamplesEndpoint(OrganizationEventsEndpointBase):
    publish_status = {
        "GET": ApiPublishStatus.PRIVATE,
    }
    snuba_methods = ["GET"]

    def get(self, request: Request, organization) -> Response:
        is_frontend = is_frontend_request(request)
        try:
            snuba_params, _ = self.get_snuba_dataclass(request, organization)
        except NoProjects:
            return Response({})

        buckets = request.GET.get("intervals", 3)
        lower_bound = request.GET.get("lowerBound", 0)
        first_bound = request.GET.get("firstBound")
        second_bound = request.GET.get("secondBound")
        upper_bound = request.GET.get("upperBound")
        column = request.GET.get("column", "span.self_time")
        selected_columns = request.GET.getlist("additionalFields", []) + [
            "project",
            "transaction.id",
            column,
            "timestamp",
            "span_id",
            "profile_id",
            "trace",
        ]

        if lower_bound is None or upper_bound is None:
            bound_results = spans_metrics.query(
                selected_columns=[
                    f"p50({column}) as first_bound",
                    f"p95({column}) as second_bound",
                ],
                params={},
                snuba_params=snuba_params,
                query=request.query_params.get("query"),
                referrer=Referrer.API_SPAN_SAMPLE_GET_BOUNDS.value,
                query_source=(QuerySource.FRONTEND if is_frontend else QuerySource.API),
            )
            if len(bound_results["data"]) != 1:
                raise ParseError("Could not find bounds")

            bound_data = bound_results["data"][0]
            first_bound, second_bound = bound_data["first_bound"], bound_data["second_bound"]
            if lower_bound == 0 or upper_bound == 0:
                raise ParseError("Could not find bounds")

        result = spans_indexed.query(
            selected_columns=[
                f"bounded_sample({column}, {lower_bound}, {first_bound}) as lower",
                f"bounded_sample({column}, {first_bound}, {second_bound}) as middle",
                f"bounded_sample({column}, {second_bound}{', ' if upper_bound else ''}{upper_bound}) as top",
                f"rounded_time({buckets})",
                "profile_id",
            ],
            orderby=["-profile_id"],
            params={},
            snuba_params=snuba_params,
            query=request.query_params.get("query"),
            referrer=Referrer.API_SPAN_SAMPLE_GET_SPAN_IDS.value,
            query_source=(QuerySource.FRONTEND if is_frontend else QuerySource.API),
        )
        span_ids = []
        for row in result["data"]:
            lower, middle, top = row["lower"], row["middle"], row["top"]
            if lower:
                span_ids.append(lower)
            if middle:
                span_ids.append(middle)
            if top:
                span_ids.append(top)

        if len(span_ids) > 0:
            query = f"span_id:[{','.join(span_ids)}] {request.query_params.get('query')}"
        else:
            query = request.query_params.get("query")

        result = spans_indexed.query(
            selected_columns=selected_columns,
            orderby=["timestamp"],
            params={},
            snuba_params=snuba_params,
            query=query,
            limit=9,
            referrer=Referrer.API_SPAN_SAMPLE_GET_SPAN_DATA.value,
            query_source=(QuerySource.FRONTEND if is_frontend else QuerySource.API),
        )
        return Response({"data": result["data"]})
