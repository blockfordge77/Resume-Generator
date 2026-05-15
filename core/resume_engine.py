from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from collections import Counter
from copy import deepcopy
from typing import Iterable

from dotenv import load_dotenv

load_dotenv()


class OpenAITraceError(Exception):
    def __init__(self, message: str, trace: dict):
        super().__init__(message)
        self.trace = trace


OPENAI_PRICING_ENV_SUFFIX = {
    'input': 'INPUT_PER_1M',
    'output': 'OUTPUT_PER_1M',
    'cached_input': 'CACHED_INPUT_PER_1M',
    'reasoning_output': 'REASONING_OUTPUT_PER_1M',
}


def _make_flow_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _safe_int(value, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _safe_float(value, default: float | None = None) -> float | None:
    try:
        if value in (None, ''):
            return default
        return float(value)
    except Exception:
        return default


def _estimate_tokens_from_text(text: str) -> int:
    raw = str(text or '')
    if not raw:
        return 0
    return max(1, math.ceil(len(raw) / 4))


def _estimate_tokens_from_payload(payload: dict) -> int:
    try:
        return _estimate_tokens_from_text(json.dumps(payload, ensure_ascii=False))
    except Exception:
        return 0


def _model_env_key(model: str, suffix: str) -> str:
    cleaned = re.sub(r'[^A-Z0-9]+', '_', str(model or '').upper()).strip('_')
    return f"OPENAI_PRICE_{cleaned}_{suffix}"


def _pricing_value_for_model(model: str, kind: str) -> tuple[float | None, str | None]:
    suffix = OPENAI_PRICING_ENV_SUFFIX.get(kind, '')
    if not suffix:
        return None, None
    specific_key = _model_env_key(model, suffix)
    specific_value = _safe_float(os.getenv(specific_key, '').strip(), None)
    if specific_value is not None:
        return specific_value, specific_key
    generic_key = f"OPENAI_PRICE_{suffix}"
    generic_value = _safe_float(os.getenv(generic_key, '').strip(), None)
    if generic_value is not None:
        return generic_value, generic_key
    return None, None


def _extract_response_usage(response) -> dict:
    usage_obj = getattr(response, 'usage', None)
    if usage_obj is None and isinstance(response, dict):
        usage_obj = response.get('usage')

    def _read(obj, *keys, default=0):
        current = obj
        for key in keys:
            if current is None:
                return default
            if isinstance(current, dict):
                current = current.get(key)
            else:
                current = getattr(current, key, None)
        if current is None:
            return default
        return _safe_int(current, default)

    input_tokens = _read(usage_obj, 'input_tokens', default=0)
    output_tokens = _read(usage_obj, 'output_tokens', default=0)
    total_tokens = _read(usage_obj, 'total_tokens', default=input_tokens + output_tokens)
    cached_tokens = _read(usage_obj, 'input_tokens_details', 'cached_tokens', default=0)
    reasoning_tokens = _read(usage_obj, 'output_tokens_details', 'reasoning_tokens', default=0)

    usage = {
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'total_tokens': total_tokens or (input_tokens + output_tokens),
        'cached_input_tokens': cached_tokens,
        'reasoning_output_tokens': reasoning_tokens,
    }
    usage['billable_input_tokens'] = max(0, usage['input_tokens'] - usage['cached_input_tokens'])
    return usage


def _estimate_usage_cost(usage: dict, model: str) -> dict:
    input_price, input_price_source = _pricing_value_for_model(model, 'input')
    output_price, output_price_source = _pricing_value_for_model(model, 'output')
    cached_price, cached_price_source = _pricing_value_for_model(model, 'cached_input')
    reasoning_price, reasoning_price_source = _pricing_value_for_model(model, 'reasoning_output')

    billable_input_tokens = _safe_int((usage or {}).get('billable_input_tokens', 0))
    cached_input_tokens = _safe_int((usage or {}).get('cached_input_tokens', 0))
    output_tokens = _safe_int((usage or {}).get('output_tokens', 0))
    reasoning_output_tokens = _safe_int((usage or {}).get('reasoning_output_tokens', 0))

    regular_output_tokens = max(0, output_tokens - reasoning_output_tokens)

    input_cost = None if input_price is None else round((billable_input_tokens / 1_000_000) * input_price, 8)
    cached_cost = None if cached_price is None else round((cached_input_tokens / 1_000_000) * cached_price, 8)
    output_cost = None if output_price is None else round((regular_output_tokens / 1_000_000) * output_price, 8)
    reasoning_cost = None if reasoning_price is None else round((reasoning_output_tokens / 1_000_000) * reasoning_price, 8)

    present_costs = [value for value in (input_cost, cached_cost, output_cost, reasoning_cost) if value is not None]
    total_cost = round(sum(present_costs), 8) if present_costs else None

    return {
        'input_cost_usd': input_cost,
        'cached_input_cost_usd': cached_cost,
        'output_cost_usd': output_cost,
        'reasoning_output_cost_usd': reasoning_cost,
        'total_cost_usd': total_cost,
        'pricing_source': {
            'input': input_price_source or '',
            'cached_input': cached_price_source or '',
            'output': output_price_source or '',
            'reasoning_output': reasoning_price_source or '',
        },
    }


def _profile_generation_summary(profile: dict) -> dict:
    clean = _profile_for_generation(profile)
    work_history = clean.get('work_history', []) or []
    education_history = clean.get('education_history', []) or []
    bullets = sum(len((item or {}).get('bullets', []) or []) for item in work_history)
    return {
        'profile_name': str(clean.get('name', '')).strip(),
        'technical_skill_count': len(clean.get('technical_skills', []) or []),
        'work_history_count': len(work_history),
        'work_history_bullet_count': bullets,
        'education_count': len(education_history),
    }


def _payload_summary_for_trace(payload: dict) -> dict:
    summary = {
        'payload_bytes': len(json.dumps(payload, ensure_ascii=False).encode('utf-8')),
        'payload_chars': len(json.dumps(payload, ensure_ascii=False)),
        'payload_estimated_tokens': _estimate_tokens_from_payload(payload),
        'job_description_chars': len(str((payload or {}).get('job_description', '') or '')),
        'default_prompt_chars': len(str((payload or {}).get('default_prompt', '') or '')),
        'custom_prompt_chars': len(str((payload or {}).get('custom_prompt', '') or '')),
        'effective_prompt_chars': len(str((payload or {}).get('effective_prompt', '') or '')),
        'target_role_chars': len(str((payload or {}).get('target_role', '') or '')),
        'validation_feedback_chars': len(str((payload or {}).get('validation_feedback', '') or '')),
        'fix_prompt_chars': len(str((payload or {}).get('fix_prompt', '') or '')),
        'question_count': len((payload or {}).get('questions', []) or []),
        'profile': _profile_generation_summary((payload or {}).get('profile', {}) or {}),
        'job_tech_keywords_count': len((((payload or {}).get('job_tech_analysis', {}) or {}).get('keywords', []) or []),),
    }
    job_tech_analysis = (payload or {}).get('job_tech_analysis', {}) or {}
    summary['job_tech_keywords_count'] = len(job_tech_analysis.get('keywords', []) or [])
    summary['job_tech_expanded_count'] = len(job_tech_analysis.get('expanded_techs', []) or [])
    summary['bullet_target_company_count'] = len((payload or {}).get('bullet_targets_per_company', []) or [])
    current_resume = (payload or {}).get('current_resume', {}) or {}
    summary['current_resume_work_history_count'] = len(current_resume.get('work_history', []) or [])
    summary['current_resume_skill_count'] = len(current_resume.get('technical_skills', []) or [])
    return summary




def _truncate_trace_text(text: str, limit: int = 120000) -> tuple[str, bool]:
    raw = str(text or '')
    if len(raw) <= limit:
        return raw, False
    return raw[:limit], True


def _response_text_for_trace(response, parsed: dict | None = None) -> tuple[str, str, bool, bool]:
    raw_text = '' if response is None else str(getattr(response, 'output_text', '') or '')
    if not raw_text and parsed is not None:
        try:
            raw_text = json.dumps(parsed, ensure_ascii=False)
        except Exception:
            raw_text = str(parsed)
    pretty_text = ''
    if parsed is not None:
        try:
            pretty_text = json.dumps(parsed, ensure_ascii=False, indent=2)
        except Exception:
            pretty_text = str(parsed)
    elif raw_text:
        try:
            pretty_text = json.dumps(json.loads(raw_text), ensure_ascii=False, indent=2)
        except Exception:
            pretty_text = raw_text
    output_text, output_text_truncated = _truncate_trace_text(raw_text)
    output_pretty, output_pretty_truncated = _truncate_trace_text(pretty_text)
    return output_text, output_pretty, output_text_truncated, output_pretty_truncated

def _build_api_trace(*, flow_id: str, call_kind: str, model: str, schema_name: str, developer_message: str, payload: dict, duration_ms: int, status: str, response=None, error: str = '', attempt: int | None = None, parsed: dict | None = None) -> dict:
    usage = _extract_response_usage(response) if response is not None else {
        'input_tokens': 0,
        'output_tokens': 0,
        'total_tokens': 0,
        'cached_input_tokens': 0,
        'reasoning_output_tokens': 0,
        'billable_input_tokens': 0,
    }
    cost = _estimate_usage_cost(usage, model)
    developer_chars = len(str(developer_message or ''))
    payload_summary = _payload_summary_for_trace(payload)
    local_input_estimate = _estimate_tokens_from_text(str(developer_message or '')) + payload_summary.get('payload_estimated_tokens', 0)
    output_text, output_pretty, output_text_truncated, output_pretty_truncated = _response_text_for_trace(response, parsed=parsed)
    trace = {
        'flow_id': flow_id,
        'call_kind': call_kind,
        'attempt': _safe_int(attempt, 0),
        'status': status,
        'model': str(model or '').strip(),
        'schema_name': str(schema_name or '').strip(),
        'response_id': '' if response is None else str(getattr(response, 'id', '') or ''),
        'duration_ms': _safe_int(duration_ms, 0),
        'developer_message_chars': developer_chars,
        'input_estimated_tokens_local': local_input_estimate,
        'output_text_chars': len(output_text),
        'output_estimated_tokens_local': _estimate_tokens_from_text(output_text),
        'output_text': output_text,
        'output_text_truncated': output_text_truncated,
        'output_pretty': output_pretty,
        'output_pretty_truncated': output_pretty_truncated,
        'usage': usage,
        'cost': cost,
        'payload_summary': payload_summary,
        'error': str(error or '').strip(),
    }
    return trace


def _openai_json_schema_call(*, client, model: str, developer_message: str, payload: dict, schema_name: str, schema: dict, flow_id: str, call_kind: str, attempt: int | None = None, user_id: str = '') -> tuple[dict, dict]:
    input_messages = [
        {'role': 'developer', 'content': developer_message},
        {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
    ]
    started = time.perf_counter()
    try:
        call_kwargs: dict = dict(
            model=model,
            input=input_messages,
            max_output_tokens=8000,
            store=False,
            text={
                'format': {
                    'type': 'json_schema',
                    'name': schema_name,
                    'strict': True,
                    'schema': schema,
                }
            },
        )
        if user_id:
            call_kwargs['user'] = user_id
        response = client.responses.create(**call_kwargs)
        duration_ms = int((time.perf_counter() - started) * 1000)
        parsed = json.loads(str(getattr(response, 'output_text', '') or ''))
        trace = _build_api_trace(
            flow_id=flow_id,
            call_kind=call_kind,
            model=model,
            schema_name=schema_name,
            developer_message=developer_message,
            payload=payload,
            duration_ms=duration_ms,
            status='success',
            response=response,
            attempt=attempt,
            parsed=parsed,
        )
        return parsed, trace
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        trace = _build_api_trace(
            flow_id=flow_id,
            call_kind=call_kind,
            model=model,
            schema_name=schema_name,
            developer_message=developer_message,
            payload=payload,
            duration_ms=duration_ms,
            status='error',
            response=None,
            error=str(exc),
            attempt=attempt,
        )
        raise OpenAITraceError(str(exc), trace) from exc


CATEGORY_ALIASES = {
    'Languages': [
        'Python', 'JavaScript', 'TypeScript', 'Java', 'Go', 'Rust', 'C#', 'C++', 'C', 'Ruby', 'PHP', 'Swift', 'Kotlin',
        'Scala', 'R', 'Bash', 'Shell', 'SQL', 'HTML', 'CSS', 'Solidity', 'Elixir', 'Dart', 'Lua',
    ],
    'Frontend': [
        'React', 'Next.js', 'Redux', 'React Query', 'Tailwind CSS', 'Material UI', 'Bootstrap', 'Storybook', 'Webpack', 'Vite',
        'Angular', 'Vue', 'Nuxt', 'Svelte', 'jQuery',
    ],
    'Backend': [
        'Node.js', 'Express', 'NestJS', 'FastAPI', 'Flask', 'Django', 'Spring Boot', 'ASP.NET Core', '.NET', 'Ruby on Rails',
        'Laravel', 'Phoenix', 'gRPC', 'GraphQL', 'Apollo GraphQL', 'Hibernate', 'Entity Framework', 'Prisma', 'SQLAlchemy',
        'RabbitMQ', 'Celery', 'REST API', 'WebSockets', 'Microservices', 'API Gateway',
    ],
    'Data': [
        'PostgreSQL', 'MySQL', 'SQL Server', 'SQLite', 'MongoDB', 'Redis', 'Elasticsearch', 'OpenSearch', 'Kafka', 'Snowflake',
        'dbt', 'Airflow', 'Spark', 'BigQuery', 'DynamoDB', 'Cassandra', 'ClickHouse', 'Redshift', 'Databricks',
    ],
    'Cloud / DevOps': [
        'AWS', 'Azure', 'GCP', 'Docker', 'Kubernetes', 'Helm', 'Terraform', 'GitHub Actions', 'GitLab CI', 'Jenkins', 'Argo CD',
        'Prometheus', 'Grafana', 'Datadog', 'Sentry', 'Nginx', 'Linux', 'Azure DevOps', 'Cloud Run', 'GKE', 'EKS', 'ECS', 'EC2',
        'S3', 'RDS', 'Lambda', 'CloudFront', 'IAM', 'Key Vault', 'Azure Functions', 'Ansible', 'Pulumi',
        'CI/CD', 'Infrastructure as Code', 'Load Balancing', 'Auto Scaling', 'Service Mesh', 'Istio',
    ],
    'Testing': [
        'Jest', 'Playwright', 'Cypress', 'React Testing Library', 'Pytest', 'JUnit', 'NUnit', 'Selenium', 'Postman',
        'Unit Testing', 'Integration Testing', 'End-to-End Testing', 'TDD', 'BDD', 'Mockito',
    ],
    'AI / Automation': [
        'OpenAI API', 'LangChain', 'LlamaIndex', 'Pinecone', 'Weaviate', 'PyTorch', 'TensorFlow', 'Hugging Face',
        'scikit-learn', 'Pandas', 'NumPy', 'MLflow', 'RAG', 'Vector Database', 'LLM',
    ],
    'Professional Skills': [
        'Team Leadership', 'Technical Leadership', 'Agile', 'Scrum', 'Kanban', 'Code Review',
        'System Design', 'Architecture Design', 'Mentoring', 'Cross-functional Collaboration',
        'Technical Documentation', 'Problem Solving', 'Communication', 'Project Management',
    ],
    'Other Relevant': [
        'Stripe', 'Twilio', 'Socket.IO', 'Auth0', 'Okta', 'Splunk', 'New Relic', 'MCP', 'FastMCP',
        'Git', 'GitHub', 'GitLab', 'Jira', 'Confluence', 'Figma', 'Swagger', 'OpenAPI',
    ],
}

KNOWN_TECH_TERMS = [
    # Languages
    'Python', 'JavaScript', 'TypeScript', 'Java', 'Go', 'Rust', 'C#', 'C++', 'C', 'Ruby', 'PHP', 'Swift', 'Kotlin',
    'Scala', 'R', 'Bash', 'Shell', 'SQL', 'HTML', 'CSS', 'Solidity', 'Elixir', 'Clojure', 'Haskell', 'Dart', 'Lua',
    # Frontend
    'React', 'Next.js', 'Redux', 'React Query', 'Tailwind CSS', 'Material UI', 'Bootstrap', 'Storybook', 'Webpack', 'Vite',
    'Angular', 'Vue', 'Nuxt', 'Svelte', 'jQuery',
    # Backend
    'Node.js', 'Express', 'NestJS', 'FastAPI', 'Flask', 'Django', 'Spring Boot', 'ASP.NET Core', '.NET', 'Ruby on Rails',
    'Laravel', 'Phoenix', 'gRPC', 'GraphQL', 'Apollo GraphQL', 'Hibernate', 'Entity Framework', 'Prisma', 'SQLAlchemy',
    'RabbitMQ', 'Celery', 'REST API', 'WebSockets', 'Microservices', 'API Gateway',
    # Data / Databases
    'PostgreSQL', 'MySQL', 'SQL Server', 'SQLite', 'MongoDB', 'Redis', 'Elasticsearch', 'OpenSearch', 'Kafka', 'Snowflake',
    'dbt', 'Airflow', 'Spark', 'BigQuery', 'DynamoDB', 'Cassandra', 'ClickHouse', 'Redshift', 'Databricks',
    # Cloud / DevOps
    'AWS', 'Azure', 'GCP', 'Docker', 'Kubernetes', 'Helm', 'Terraform', 'GitHub Actions', 'GitLab CI', 'Jenkins', 'Argo CD',
    'Prometheus', 'Grafana', 'Datadog', 'Sentry', 'Nginx', 'Linux', 'Azure DevOps', 'Cloud Run', 'GKE', 'EKS', 'ECS', 'EC2',
    'S3', 'RDS', 'Lambda', 'CloudFront', 'IAM', 'Key Vault', 'Azure Functions', 'Ansible', 'Pulumi', 'Vagrant',
    'CI/CD', 'Infrastructure as Code', 'Load Balancing', 'Auto Scaling', 'Service Mesh', 'Istio',
    # Testing
    'Jest', 'Playwright', 'Cypress', 'React Testing Library', 'Pytest', 'JUnit', 'NUnit', 'Selenium', 'Postman',
    'Unit Testing', 'Integration Testing', 'End-to-End Testing', 'TDD', 'BDD', 'Mockito',
    # AI / ML
    'OpenAI API', 'LangChain', 'LlamaIndex', 'Pinecone', 'Weaviate', 'PyTorch', 'TensorFlow', 'Hugging Face',
    'scikit-learn', 'Pandas', 'NumPy', 'Jupyter', 'MLflow', 'RAG', 'Vector Database', 'LLM',
    # Other tools
    'Stripe', 'Twilio', 'Socket.IO', 'Auth0', 'Okta', 'Splunk', 'New Relic', 'MCP', 'FastMCP',
    'Git', 'GitHub', 'GitLab', 'Jira', 'Confluence', 'Figma', 'Swagger', 'OpenAPI',
    # Professional / soft skills
    'Team Leadership', 'Technical Leadership', 'Agile', 'Scrum', 'Kanban', 'Code Review',
    'System Design', 'Architecture Design', 'Mentoring', 'Cross-functional Collaboration',
    'Technical Documentation', 'Problem Solving', 'Communication', 'Project Management',
]

TECH_ALIAS_MAP = {
    'reactjs': 'React', 'react.js': 'React', 'react': 'React',
    'next': 'Next.js', 'nextjs': 'Next.js', 'next.js': 'Next.js',
    'redux toolkit': 'Redux', 'redux': 'Redux',
    'react-query': 'React Query', 'tanstack query': 'React Query', 'react query': 'React Query',
    'tailwind': 'Tailwind CSS', 'tailwindcss': 'Tailwind CSS',
    'mui': 'Material UI', 'material-ui': 'Material UI', 'material ui': 'Material UI',
    'angularjs': 'Angular', 'angular': 'Angular', 'vue.js': 'Vue', 'vuejs': 'Vue', 'vue': 'Vue', 'nuxt.js': 'Nuxt', 'nuxt': 'Nuxt',
    'node': 'Node.js', 'nodejs': 'Node.js', 'node.js': 'Node.js',
    'express.js': 'Express', 'express': 'Express', 'nestjs': 'NestJS', 'nest.js': 'NestJS', 'nest': 'NestJS',
    'fast api': 'FastAPI', 'fastapi': 'FastAPI', 'flask': 'Flask', 'django': 'Django',
    'spring': 'Spring Boot', 'springboot': 'Spring Boot', 'spring boot': 'Spring Boot',
    'asp.net': 'ASP.NET Core', 'asp.net core': 'ASP.NET Core', 'dotnet': '.NET', '.net': '.NET',
    'rails': 'Ruby on Rails', 'ruby on rails': 'Ruby on Rails', 'laravel': 'Laravel', 'phoenix': 'Phoenix',
    'graphql': 'GraphQL', 'apollo': 'Apollo GraphQL', 'apollo graphql': 'Apollo GraphQL', 'grpc': 'gRPC',
    'entity framework': 'Entity Framework', 'hibernate': 'Hibernate', 'prisma': 'Prisma', 'sqlalchemy': 'SQLAlchemy',
    'rabbitmq': 'RabbitMQ', 'celery': 'Celery',
    'postgres': 'PostgreSQL', 'postgresql': 'PostgreSQL', 'mysql': 'MySQL', 'sql server': 'SQL Server', 'sqlite': 'SQLite',
    'mongodb': 'MongoDB', 'mongo': 'MongoDB', 'redis': 'Redis', 'elasticsearch': 'Elasticsearch', 'opensearch': 'OpenSearch',
    'kafka': 'Kafka', 'snowflake': 'Snowflake', 'dbt': 'dbt', 'airflow': 'Airflow', 'spark': 'Spark', 'bigquery': 'BigQuery',
    'dynamodb': 'DynamoDB',
    'aws': 'AWS', 'amazon web services': 'AWS', 'azure': 'Azure', 'gcp': 'GCP', 'google cloud': 'GCP',
    'docker': 'Docker', 'k8s': 'Kubernetes', 'kubernetes': 'Kubernetes', 'helm': 'Helm', 'terraform': 'Terraform',
    'github actions': 'GitHub Actions', 'gitlab ci': 'GitLab CI', 'jenkins': 'Jenkins', 'argocd': 'Argo CD', 'argo cd': 'Argo CD',
    'prometheus': 'Prometheus', 'grafana': 'Grafana', 'datadog': 'Datadog', 'sentry': 'Sentry', 'nginx': 'Nginx', 'linux': 'Linux',
    'azure devops': 'Azure DevOps', 'cloud run': 'Cloud Run', 'gke': 'GKE', 'eks': 'EKS', 'ecs': 'ECS', 'ec2': 'EC2', 's3': 'S3',
    'rds': 'RDS', 'lambda': 'Lambda', 'cloudfront': 'CloudFront', 'iam': 'IAM', 'key vault': 'Key Vault', 'azure functions': 'Azure Functions',
    'jest': 'Jest', 'playwright': 'Playwright', 'cypress': 'Cypress', 'react testing library': 'React Testing Library',
    'pytest': 'Pytest', 'junit': 'JUnit', 'nunit': 'NUnit', 'selenium': 'Selenium', 'postman': 'Postman',
    'openai': 'OpenAI API', 'openai api': 'OpenAI API', 'langchain': 'LangChain', 'llamaindex': 'LlamaIndex',
    'pinecone': 'Pinecone', 'weaviate': 'Weaviate', 'pytorch': 'PyTorch', 'tensorflow': 'TensorFlow', 'huggingface': 'Hugging Face',
    'hugging face': 'Hugging Face',
    'stripe': 'Stripe', 'twilio': 'Twilio', 'socket.io': 'Socket.IO', 'auth0': 'Auth0', 'okta': 'Okta', 'splunk': 'Splunk',
    'new relic': 'New Relic', 'mcp': 'MCP', 'fastmcp': 'FastMCP'
}

TECH_EXPANSION_MAP = {
    'React': ['Next.js', 'Redux', 'React Query', 'Tailwind CSS', 'Material UI', 'Storybook', 'Jest', 'Playwright', 'React Testing Library', 'Vite'],
    'Angular': ['TypeScript', 'RxJS', 'NgRx', 'Jest', 'Cypress'],
    'Vue': ['Nuxt', 'Pinia', 'Vite', 'Jest', 'Cypress'],
    'Node.js': ['Express', 'NestJS', 'GraphQL', 'Apollo GraphQL', 'PostgreSQL', 'Redis', 'Docker', 'Jest'],
    'Express': ['Node.js', 'PostgreSQL', 'Redis', 'Docker', 'Jest', 'Postman'],
    'NestJS': ['Node.js', 'PostgreSQL', 'Redis', 'Docker', 'Jest', 'Postman'],
    'FastAPI': ['Python', 'PostgreSQL', 'Redis', 'Docker', 'Pytest', 'Postman'],
    'Django': ['PostgreSQL', 'Redis', 'Docker', 'Pytest'],
    'Flask': ['PostgreSQL', 'Redis', 'Docker', 'Pytest'],
    'Spring Boot': ['Java', 'PostgreSQL', 'Kafka', 'Docker', 'Kubernetes', 'JUnit', 'Hibernate'],
    'ASP.NET Core': ['.NET', 'SQL Server', 'Azure', 'Docker', 'NUnit', 'Entity Framework'],
    'GraphQL': ['Apollo GraphQL', 'PostgreSQL', 'Redis', 'Node.js'],
    'Kafka': ['Spark', 'Docker', 'Kubernetes', 'Prometheus', 'Grafana'],
    'AWS': ['EC2', 'S3', 'Lambda', 'RDS', 'EKS', 'ECS', 'CloudFront', 'IAM', 'Terraform', 'GitHub Actions'],
    'Azure': ['Azure DevOps', 'Azure Functions', 'Key Vault', '.NET', 'Terraform', 'Docker'],
    'GCP': ['Cloud Run', 'GKE', 'BigQuery', 'Terraform', 'Docker'],
    'Docker': ['Kubernetes', 'Helm', 'Terraform', 'GitHub Actions', 'GitLab CI', 'Jenkins'],
    'Kubernetes': ['Helm', 'Terraform', 'Prometheus', 'Grafana', 'Argo CD'],
    'OpenAI API': ['LangChain', 'LlamaIndex', 'Pinecone', 'Weaviate', 'PyTorch', 'Hugging Face'],
    'PyTorch': ['TensorFlow', 'Hugging Face', 'OpenAI API'],
    'PostgreSQL': ['Redis', 'Elasticsearch', 'Kafka', 'Docker'],
    'Snowflake': ['dbt', 'Airflow', 'Spark', 'BigQuery'],
}

ROLE_TECH_STACKS = {
    'Frontend Engineer': ['React', 'Next.js', 'Redux', 'React Query', 'Tailwind CSS', 'Material UI', 'Storybook', 'Vite', 'Webpack', 'Jest', 'Playwright', 'Cypress', 'React Testing Library'],
    'Backend Engineer': ['Node.js', 'Express', 'NestJS', 'FastAPI', 'Django', 'Spring Boot', 'ASP.NET Core', 'GraphQL', 'Apollo GraphQL', 'gRPC', 'PostgreSQL', 'MySQL', 'Redis', 'Kafka', 'Docker', 'Kubernetes', 'Terraform', 'GitHub Actions', 'Datadog', 'Sentry'],
    'Full Stack Engineer': ['React', 'Next.js', 'Tailwind CSS', 'Material UI', 'Node.js', 'Express', 'NestJS', 'FastAPI', 'GraphQL', 'Apollo GraphQL', 'PostgreSQL', 'Redis', 'Docker', 'Kubernetes', 'AWS', 'GitHub Actions', 'Jest', 'Playwright'],
    'Data Engineer': ['Airflow', 'dbt', 'Spark', 'Kafka', 'Snowflake', 'BigQuery', 'PostgreSQL', 'MySQL', 'Redis', 'Docker', 'Kubernetes', 'Terraform', 'AWS', 'GCP', 'Datadog'],
    'DevOps Engineer': ['AWS', 'Azure', 'GCP', 'Docker', 'Kubernetes', 'Helm', 'Terraform', 'GitHub Actions', 'GitLab CI', 'Jenkins', 'Argo CD', 'Prometheus', 'Grafana', 'Datadog', 'Sentry', 'Linux', 'Nginx'],
    'Machine Learning Engineer': ['OpenAI API', 'LangChain', 'LlamaIndex', 'Pinecone', 'Weaviate', 'PyTorch', 'TensorFlow', 'Hugging Face', 'Airflow', 'Spark', 'Docker', 'Kubernetes', 'AWS', 'GCP', 'PostgreSQL'],
    'MLOps Engineer': ['OpenAI API', 'LangChain', 'Pinecone', 'Weaviate', 'PyTorch', 'TensorFlow', 'Docker', 'Kubernetes', 'Terraform', 'GitHub Actions', 'Prometheus', 'Grafana', 'AWS', 'GCP'],
    'Platform Engineer': ['Docker', 'Kubernetes', 'Helm', 'Terraform', 'GitHub Actions', 'GitLab CI', 'Jenkins', 'Argo CD', 'Prometheus', 'Grafana', 'Datadog', 'AWS', 'Azure', 'GCP', 'Linux'],
    'Site Reliability Engineer': ['AWS', 'Azure', 'GCP', 'Docker', 'Kubernetes', 'Terraform', 'Prometheus', 'Grafana', 'Datadog', 'Sentry', 'Nginx', 'Linux', 'Argo CD'],
}

ROLE_HINTS = [
    ('Machine Learning Engineer', ['machine learning', 'ml', 'pytorch', 'tensorflow', 'llm', 'nlp', 'rag', 'model training', 'inference', 'fine-tuning']),
    ('MLOps Engineer', ['mlops', 'model deployment', 'ml pipeline', 'inference', 'feature store']),
    ('DevOps Engineer', ['devops', 'terraform', 'kubernetes', 'helm', 'docker', 'ci/cd', 'jenkins', 'github actions', 'observability', 'infrastructure']),
    ('Site Reliability Engineer', ['sre', 'reliability', 'incident', 'observability', 'monitoring', 'availability']),
    ('Data Engineer', ['data engineer', 'etl', 'elt', 'airflow', 'spark', 'warehouse', 'snowflake', 'dbt', 'pipeline']),
    ('Backend Engineer', ['backend', 'api', 'microservices', 'spring boot', 'java', 'fastapi', 'node.js', 'distributed systems']),
    ('Frontend Engineer', ['frontend', 'react', 'typescript', 'vue', 'angular', 'ui', 'web accessibility']),
    ('Platform Engineer', ['platform', 'developer tooling', 'kubernetes', 'terraform', 'internal tools']),
    ('Full Stack Engineer', ['full stack', 'react', 'node.js', 'typescript', 'python', 'api', 'frontend', 'backend']),
]


APPLICATION_ANSWER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "question": {"type": "string"},
                    "answer": {"type": "string"},
                },
                "required": ["question", "answer"],
            },
        }
    },
    "required": ["answers"],
}

RESUME_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'headline': {'type': 'string'},
        'summary': {'type': 'string'},
        'skill_groups': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'category': {'type': 'string'},
                    'items': {'type': 'array', 'items': {'type': 'string'}},
                },
                'required': ['category', 'items'],
            },
        },
        'work_history': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'company_index': {'type': 'integer'},
                    'role_title': {'type': 'string'},
                    'bullets': {'type': 'array', 'items': {'type': 'string'}},
                },
                'required': ['company_index', 'role_title', 'bullets'],
            },
        },
    },
    'required': ['headline', 'summary', 'skill_groups', 'work_history'],
}


def generate_resume_content(
    profile: dict,
    job_description: str,
    target_role: str = '',
    default_prompt: str = '',
    use_ai: bool = True,
    clean_generation: bool = True,
    model: str = '',
) -> dict:
    job_tech_analysis = _analyze_job_tech_stack(job_description, target_role=target_role)
    api_key = os.getenv('OPENAI_API_KEY', '').strip()
    attempts = []
    api_logs: list[dict] = []
    flow_id = _make_flow_id('resume_generate')

    profile_bullet_counts = (profile.get('generation_settings') or {}).get('bullet_counts') or []

    if use_ai and api_key:
        try:
            call_result = _generate_with_openai(
                profile=profile,
                job_description=job_description,
                target_role=target_role,
                default_prompt=default_prompt,
                job_tech_analysis=job_tech_analysis,
                validation_feedback='',
                flow_id=flow_id,
                attempt=1,
                model=model,
            )
            resume = call_result['resume']
            api_logs.append(call_result['api_log'])
            validation = _resume_meets_generation_requirements(resume, job_tech_analysis, bullet_counts=profile_bullet_counts)
            attempts.append({'attempt': 1, 'validation': validation})
            api_logs[-1]['post_validation'] = validation
            return {'mode': 'openai', 'resume': resume, 'job_tech_analysis': job_tech_analysis, 'attempts': attempts, 'api_logs': api_logs, 'flow_id': flow_id}
        except OpenAITraceError as exc:  # pragma: no cover
            if getattr(exc, 'trace', None):
                api_logs.append(exc.trace)
        except Exception:  # pragma: no cover
            pass
        resume = _generate_demo_resume(
            profile=profile,
            job_description=job_description,
            target_role=target_role,
            default_prompt=default_prompt,
            clean_generation=clean_generation,
            job_tech_analysis=job_tech_analysis,
        )
        resume['generation_note'] = 'Fell back to demo mode because OpenAI request failed.'
        return {'mode': 'demo-fallback', 'resume': resume, 'job_tech_analysis': job_tech_analysis, 'attempts': attempts, 'api_logs': api_logs, 'flow_id': flow_id}

    resume = _generate_demo_resume(
        profile=profile,
        job_description=job_description,
        target_role=target_role,
        default_prompt=default_prompt,
        clean_generation=clean_generation,
        job_tech_analysis=job_tech_analysis,
    )
    return {'mode': 'demo', 'resume': resume, 'job_tech_analysis': job_tech_analysis, 'attempts': attempts, 'api_logs': api_logs, 'flow_id': flow_id}


def update_resume_content(
    profile: dict,
    job_description: str,
    current_resume: dict,
    fix_prompt: str,
    target_role: str = '',
    default_prompt: str = '',
    use_ai: bool = True,
    clean_generation: bool = True,
    flow_id: str | None = None,
    model: str = '',
) -> dict:
    current_resume = deepcopy(current_resume or {})
    api_key = os.getenv('OPENAI_API_KEY', '').strip()
    api_logs: list[dict] = []
    effective_flow_id = flow_id or _make_flow_id('resume_update')
    if use_ai and api_key:
        try:
            call_result = _update_with_openai(
                profile=profile,
                job_description=job_description,
                current_resume=current_resume,
                fix_prompt=fix_prompt,
                target_role=target_role,
                default_prompt=default_prompt,
                clean_generation=clean_generation,
                flow_id=effective_flow_id,
                model=model,
            )
            api_logs.append(call_result['api_log'])
            return {'mode': 'openai-update', 'resume': call_result['resume'], 'api_logs': api_logs, 'flow_id': effective_flow_id}
        except OpenAITraceError as exc:  # pragma: no cover
            if getattr(exc, 'trace', None):
                api_logs.append(exc.trace)
            resume = _update_demo_resume(
                profile=profile,
                job_description=job_description,
                current_resume=current_resume,
                fix_prompt=fix_prompt,
                target_role=target_role,
            )
            resume['generation_note'] = f'Fell back to demo update because OpenAI request failed: {exc}'
            return {'mode': 'demo-update-fallback', 'resume': resume, 'api_logs': api_logs, 'flow_id': effective_flow_id}
        except Exception as exc:  # pragma: no cover
            resume = _update_demo_resume(
                profile=profile,
                job_description=job_description,
                current_resume=current_resume,
                fix_prompt=fix_prompt,
                target_role=target_role,
            )
            resume['generation_note'] = f'Fell back to demo update because OpenAI request failed: {exc}'
            return {'mode': 'demo-update-fallback', 'resume': resume, 'api_logs': api_logs, 'flow_id': effective_flow_id}

    return {
        'mode': 'demo-update',
        'resume': _update_demo_resume(
            profile=profile,
            job_description=job_description,
            current_resume=current_resume,
            fix_prompt=fix_prompt,
            target_role=target_role,
        ),
        'api_logs': api_logs,
        'flow_id': effective_flow_id,
    }


def improve_resume_to_target_ats(
    profile: dict,
    job_description: str,
    current_resume: dict,
    target_score: int = 91,
    max_rounds: int = 3,
    additional_requirements: str = '',
    target_role: str = '',
    default_prompt: str = '',
    use_ai: bool = True,
    clean_generation: bool = True,
    model: str = '',
) -> dict:
    working_resume = deepcopy(current_resume or {})
    history: list[dict] = []
    api_logs: list[dict] = []
    flow_id = _make_flow_id('ats_improve')
    best_resume = deepcopy(working_resume)
    best_analysis = analyze_ats_score(best_resume, job_description, target_role=target_role)

    if best_analysis.get('overall_score', 0) >= target_score:
        return {
            'mode': 'ats-already-met',
            'resume': best_resume,
            'history': history,
            'final_analysis': best_analysis,
            'api_logs': api_logs,
            'flow_id': flow_id,
        }

    latest_mode = 'ats-auto-improve'
    for round_num in range(1, max(1, int(max_rounds)) + 1):
        before_analysis = analyze_ats_score(working_resume, job_description, target_role=target_role)
        if before_analysis.get('overall_score', 0) >= target_score:
            best_resume = deepcopy(working_resume)
            best_analysis = before_analysis
            break

        fix_prompt = _build_ats_fix_prompt(before_analysis, target_score=target_score, additional_requirements=additional_requirements)
        update_result = update_resume_content(
            profile=profile,
            job_description=job_description,
            current_resume=working_resume,
            fix_prompt=fix_prompt,
            target_role=target_role,
            default_prompt=default_prompt,
            use_ai=use_ai,
            clean_generation=clean_generation,
            flow_id=flow_id,
            model=model,
        )
        latest_mode = update_result.get('mode', latest_mode)
        api_logs.extend(update_result.get('api_logs', []) or [])
        candidate_resume = deepcopy(update_result.get('resume') or working_resume)
        after_analysis = analyze_ats_score(candidate_resume, job_description, target_role=target_role)

        history.append({
            'round': round_num,
            'before_score': before_analysis.get('overall_score', 0),
            'after_score': after_analysis.get('overall_score', 0),
            'mode': update_result.get('mode', ''),
            'used_suggestions': before_analysis.get('suggestions', []),
            'fix_prompt': fix_prompt,
        })

        if after_analysis.get('overall_score', 0) >= best_analysis.get('overall_score', 0):
            best_resume = deepcopy(candidate_resume)
            best_analysis = after_analysis
            working_resume = deepcopy(candidate_resume)
        else:
            working_resume = deepcopy(best_resume)

        if best_analysis.get('overall_score', 0) >= target_score:
            break

    return {
        'mode': latest_mode if history else 'ats-auto-improve',
        'resume': best_resume,
        'history': history,
        'final_analysis': best_analysis,
        'api_logs': api_logs,
        'flow_id': flow_id,
    }


def analyze_ats_score(resume: dict, job_description: str, target_role: str = '') -> dict:
    resume = deepcopy(resume or {})
    job_description = job_description or ''
    keywords = _extract_keywords(job_description)
    if target_role:
        keywords = _dedupe_preserve_order([target_role, *keywords])

    headline = str(resume.get('headline', '')).strip()
    summary = str(resume.get('summary', '')).strip()
    technical_skills = [str(item).strip() for item in resume.get('technical_skills', []) if str(item).strip()]
    fit_keywords = [str(item).strip() for item in resume.get('fit_keywords', []) if str(item).strip()]
    work_history = resume.get('work_history', []) or []
    education_history = resume.get('education_history', []) or []

    resume_text_parts = [headline, summary]
    resume_text_parts.extend(technical_skills)
    resume_text_parts.extend(fit_keywords)
    for group in resume.get('skill_groups', []) or []:
        resume_text_parts.append(group.get('category', ''))
        resume_text_parts.extend(group.get('items', []))
    for job in work_history:
        resume_text_parts.extend([
            job.get('company_name', ''),
            job.get('role_title', ''),
            job.get('role_headline', ''),
            *job.get('bullets', []),
        ])
    resume_blob = ' '.join(str(part) for part in resume_text_parts if str(part).strip())
    resume_blob_lower = resume_blob.lower()

    top_keywords = keywords[:14]
    matched_keywords = [kw for kw in top_keywords if kw and kw.lower() in resume_blob_lower]
    missing_keywords = [kw for kw in top_keywords if kw and kw.lower() not in resume_blob_lower]
    coverage_ratio = len(matched_keywords) / max(len(top_keywords), 1)
    keyword_score = round(30 * coverage_ratio)

    inferred_title = _infer_target_title(target_role, keywords, technical_skills)
    title_alignment = 0
    headline_lower = headline.lower()
    if inferred_title and inferred_title.lower() in headline_lower:
        title_alignment += 10
    elif inferred_title and any(part.lower() in headline_lower for part in inferred_title.split() if len(part) > 3):
        title_alignment += 7
    if any(kw.lower() in headline_lower for kw in matched_keywords[:4]):
        title_alignment += 5
    if work_history and inferred_title and any(inferred_title.lower() in str(job.get('role_title', '')).lower() for job in work_history):
        title_alignment += 3
    title_alignment = min(title_alignment, 15)

    exact_skill_matches = [skill for skill in technical_skills if any(skill.lower() == kw.lower() for kw in keywords)]
    grouped_skill_matches = [kw for kw in matched_keywords if any(kw.lower() == str(item).lower() for group in resume.get('skill_groups', []) or [] for item in group.get('items', []))]
    skill_score = min(15, 6 + len(_dedupe_preserve_order(exact_skill_matches + grouped_skill_matches))) if (technical_skills or resume.get('skill_groups')) else 0

    bullets = [bullet for job in work_history for bullet in job.get('bullets', []) if str(bullet).strip()]
    experience_hits = sum(1 for bullet in bullets if any(kw.lower() in bullet.lower() for kw in top_keywords))
    tech_named_hits = sum(1 for bullet in bullets if any(term.lower() in bullet.lower() for term in KNOWN_TECH_TERMS))
    bullet_depth_bonus = 3 if len(bullets) >= max(4, len(work_history) * 3) else 0
    experience_score = 0
    if bullets:
        experience_score = min(20, 8 + min(experience_hits, 8) + min(tech_named_hits, 4) + bullet_depth_bonus)

    format_score = 0
    if headline:
        format_score += 2
    if summary:
        format_score += 2
    if technical_skills or resume.get('skill_groups'):
        format_score += 2
    if work_history:
        format_score += 3
    if education_history:
        format_score += 1

    summary_score = 0
    summary_len = len(summary)
    if 180 <= summary_len <= 650:
        summary_score = 5
    elif summary_len > 0:
        summary_score = 3

    fit_bonus = 0
    if coverage_ratio >= 0.55:
        fit_bonus += 6
    elif coverage_ratio >= 0.4:
        fit_bonus += 3
    if len(exact_skill_matches) >= 4:
        fit_bonus += 3
    elif len(exact_skill_matches) >= 2:
        fit_bonus += 1
    if experience_hits >= max(4, len(work_history) * 2):
        fit_bonus += 3
    elif experience_hits >= max(2, len(work_history)):
        fit_bonus += 1
    fit_bonus = min(fit_bonus, 10)

    overall = max(0, min(99, keyword_score + title_alignment + skill_score + experience_score + format_score + summary_score + fit_bonus))

    if all([headline, summary, work_history, education_history]) and coverage_ratio >= 0.55 and len(exact_skill_matches) >= 3 and experience_hits >= max(4, len(work_history) * 2):
        overall = max(overall, 91)
    elif all([headline, summary, work_history]) and coverage_ratio >= 0.45 and len(exact_skill_matches) >= 2 and experience_hits >= max(3, len(work_history)):
        overall = max(overall, 88)

    strengths: list[str] = []
    if matched_keywords:
        strengths.append(f'Matches {len(matched_keywords)} of the top {len(top_keywords)} job keywords, including {", ".join(matched_keywords[:5])}.')
    if headline:
        strengths.append(f'Headline is tailored toward {headline}.')
    if bullets:
        strengths.append(f'Work history contains {len(bullets)} bullets with concrete role evidence and named stack coverage.')
    if technical_skills or resume.get('skill_groups'):
        strengths.append(f'Technical skills section names relevant stacks directly, including {", ".join((technical_skills or fit_keywords)[:6])}.')

    risks: list[str] = []
    if missing_keywords:
        risks.append(f'Missing or underused JD terms: {", ".join(missing_keywords[:8])}.')
    if not bullets:
        risks.append('Work history needs bullets with concrete responsibilities and technologies.')
    elif experience_hits < max(3, len(work_history)):
        risks.append('Several bullets are still light on exact job-description technologies or domain terms.')
    if summary_len < 180:
        risks.append('Summary is short and may not surface enough positioning or tech focus.')
    if not exact_skill_matches:
        risks.append('Skills section does not show many exact keyword matches from the job description.')

    suggestions: list[str] = []
    if missing_keywords:
        suggestions.append(f'Add supported missing keywords into skills or bullets where truthful: {", ".join(missing_keywords[:6])}.')
    suggestions.append('Keep headline, summary, and first two bullets aligned to the same core role focus.')
    suggestions.append('Prefer exact stack names in bullets instead of broad phrases like backend services or cloud systems.')
    suggestions.append('Use role headlines and bullet openings to mirror the strongest JD themes.')

    category_scores = {
        'Keyword Match': keyword_score,
        'Title Alignment': title_alignment,
        'Skills Alignment': skill_score,
        'Experience Evidence': experience_score,
        'Structure': format_score + summary_score,
        'Fit Bonus': fit_bonus,
    }

    return {
        'overall_score': overall,
        'inferred_target_title': inferred_title,
        'matched_keywords': matched_keywords,
        'missing_keywords': missing_keywords,
        'category_scores': category_scores,
        'strengths': strengths[:4],
        'risks': risks[:4],
        'suggestions': suggestions[:4],
    }


def _build_ats_fix_prompt(analysis: dict, target_score: int = 91, additional_requirements: str = '') -> str:
    suggestion_lines = analysis.get('suggestions', []) or []
    risk_lines = analysis.get('risks', []) or []
    missing_keywords = analysis.get('missing_keywords', []) or []

    parts = [
        f'Improve this resume until its ATS score reaches at least {int(target_score)} if the source profile supports it.',
        'Prioritize the ATS gaps first and keep all edits truthful to the source profile.',
        'Strengthen the headline, summary, skills, role titles, role headlines, and bullets so the draft matches the job description more directly.',
        'Prefer exact named technologies and domain terms over generic wording whenever they are supported by the profile.',
    ]
    if missing_keywords:
        parts.append('Add supported missing JD keywords where they fit naturally: ' + ', '.join(missing_keywords[:8]) + '.')
    if suggestion_lines:
        parts.append('Apply these ATS improvement suggestions: ' + ' '.join(suggestion_lines[:4]))
    if risk_lines:
        parts.append('Address these risks: ' + ' '.join(risk_lines[:3]))
    if additional_requirements.strip():
        parts.append('Additional user requirements: ' + additional_requirements.strip())
    return ' '.join(parts)


def generate_application_answers(
    resume: dict,
    job_description: str,
    questions: list[str],
    target_role: str = '',
    use_ai: bool = True,
    model: str = '',
) -> dict:
    clean_questions = [str(question).strip() for question in questions if str(question).strip()]
    if not clean_questions:
        return {'mode': 'empty', 'answers': [], 'api_logs': [], 'flow_id': _make_flow_id('application_answers')}

    api_key = os.getenv('OPENAI_API_KEY', '').strip()
    flow_id = _make_flow_id('application_answers')
    if use_ai and api_key:
        try:
            call_result = _generate_answers_with_openai(resume, job_description, clean_questions, target_role, flow_id=flow_id, model=model)
            return {'mode': 'openai', 'answers': call_result['answers'], 'api_logs': [call_result['api_log']], 'flow_id': flow_id}
        except OpenAITraceError as exc:  # pragma: no cover
            answers = _generate_demo_answers(resume, job_description, clean_questions, target_role)
            for item in answers:
                item['note'] = f'Fell back to demo mode because OpenAI request failed: {exc}'
            return {'mode': 'demo-fallback', 'answers': answers, 'api_logs': [exc.trace] if getattr(exc, 'trace', None) else [], 'flow_id': flow_id}
        except Exception as exc:  # pragma: no cover
            answers = _generate_demo_answers(resume, job_description, clean_questions, target_role)
            for item in answers:
                item['note'] = f'Fell back to demo mode because OpenAI request failed: {exc}'
            return {'mode': 'demo-fallback', 'answers': answers, 'api_logs': [], 'flow_id': flow_id}

    return {'mode': 'demo', 'answers': _generate_demo_answers(resume, job_description, clean_questions, target_role), 'api_logs': [], 'flow_id': flow_id}


def _generate_with_openai(profile: dict, job_description: str, target_role: str, default_prompt: str, job_tech_analysis: dict | None = None, validation_feedback: str = '', flow_id: str = '', attempt: int = 1, model: str = '') -> dict:
    from openai import OpenAI

    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
    model = model or os.getenv('OPENAI_MODEL', 'gpt-5.1')
    profile_id = str(profile.get('id') or '').strip()
    profile_name = str(profile.get('name') or '').strip()

    work_history = profile.get('work_history', []) or []
    work_history_count = len(work_history)
    gen_settings = profile.get('generation_settings') or {}
    bullet_counts = gen_settings.get('bullet_counts') or []

    def _bullet_target(idx: int) -> int:
        if idx < len(bullet_counts):
            try:
                return int(bullet_counts[idx])
            except Exception:
                pass
        return _target_bullet_count(idx, work_history_count)

    bullet_targets = ', '.join(
        f"company {idx + 1} (index {idx}) = exactly {_bullet_target(idx)} bullets"
        for idx in range(work_history_count)
    ) or 'each company 10 to 15 bullets'

    total_years = profile.get('total_years_of_experience')
    summary_char_count = gen_settings.get('summary_char_count')
    skills_count = gen_settings.get('skills_count') or 85
    skills_min = max(80, skills_count - 5)
    skills_max = skills_count + 5

    if total_years:
        years_rule = f'The candidate has {total_years}+ years of experience. You MUST state exactly "{total_years}+ years of experience" in the professional summary.'
    else:
        years_rule = 'Calculate the candidate total years of experience by counting from the earliest company start date in the work history to the present, and state it accurately in the summary (e.g., "12+ years of experience").'

    if summary_char_count:
        summary_rule = f'The professional summary must be flowing prose (no bullets), between {summary_char_count - 80} and {summary_char_count + 80} characters total.'
    else:
        summary_rule = 'The professional summary must be 7 to 9 sentences of flowing prose (no bullets), roughly 800 to 1100 characters.'

    identity_rule = (
        f'This resume is exclusively for {profile_name} (profile_id={profile_id}). '
        f'Write every sentence — summary, bullets, skills — as if you have never written a resume before. '
        f'Do NOT reuse any wording, sentence structure, bullet pattern, or phrasing from any prior generation. '
        f'The voice, vocabulary, and technical emphasis must be unique to this specific candidate and this specific job. '
    ) if profile_name else ''

    developer_message = (
        'You are a senior resume writer and ATS optimizer. Build the resume from a clean slate every time. '
        'Do not rely on previous resumes, previous generations, examples, or any historical context outside the current request payload. '
        'Use only the current work history, job description, and prompt guidance provided in the payload. '
        f'{identity_rule}'

        'STEP 1 — TECHNOLOGY TIMELINE ENFORCEMENT (do this before writing anything): '
        'For each company in the work history, note its date range. Then apply these rules strictly: '
        'Never mention AI/ML, LLMs, vector databases, LangChain, or OpenAI SDK in any role ending before 2021. '
        'Never mention Tailwind CSS, React Query, Vite, dbt, Snowflake, or Playwright in any role ending before 2020. '
        'Never mention Kubernetes, GraphQL, gRPC, or Kafka in any role ending before 2018. '
        'Never mention Django or FastAPI in any role ending before 2019. '
        'Never mention NestJS in any role ending before 2019. '
        'Never mention React Hooks patterns in any role ending before 2019. '
        'Never mention Docker or TypeScript as mainstream in any role ending before 2016. '
        'Violating these rules is a hard error — the resume will be rejected. '

        'STEP 2 — TECH STACK EXPANSION: '
        f'Analyze the job description and expand into {skills_min} to {skills_max} deduplicated technical items: '
        'frameworks, libraries, platforms, cloud services, databases, developer tools, testing tools, CI/CD tools, observability tools, and AI tooling. '
        'No soft skills, generic concepts, or vague architecture labels. '
        'Return skill_groups with categories: Languages, Frontend, Backend, Databases, Cloud & Infrastructure, DevOps & CI/CD, Testing, Observability & Monitoring, Security, Messaging & Streaming, AI/ML & Data. '
        'Each category must have at least 10 items. Pad with closely related tools if needed to reach 10. '

        'STEP 3 — PROFESSIONAL SUMMARY: '
        f'{years_rule} '
        f'{summary_rule} '
        'Include 8 to 10 required technologies from the JD. '
        'The summary must read as written by a human senior engineer — specific, grounded, no generic filler. '

        'STEP 4 — ROLE TITLES: '
        'The work history does not include role titles. Infer the most believable role_title for each company from the job description and bullet evidence. '
        'Titles may be: Machine Learning Engineer, DevOps Engineer, Platform Engineer, Data Engineer, Backend Engineer, Frontend Engineer, Full Stack Engineer, or another job-aligned title. '
        'Make headline and all role titles sharply aligned to the core function of the job description. '

        'STEP 5 — BULLETS (critical — follow exactly): '
        f'Per-company bullet targets: {bullet_targets}. '
        'You MUST produce exactly the specified number of bullets for each company — not one more, not one fewer. '
        'BULLET LENGTH RULE (hard constraint): every bullet must be between 120 and 175 characters long (count the characters). '
        'This range guarantees exactly 2 printed lines on the document — no bullet may be shorter than 120 chars (would be 1 line) or longer than 175 chars (would be 3 lines). '
        'Count characters before finalising each bullet and adjust if out of range. '
        'Each bullet must contain: a strong action verb + specific technology/tool + measurable outcome or scale. '
        'Apply timeline rules from STEP 1 — if a technology was not mainstream during a role\'s date range, do NOT use it in that role\'s bullets. '
        'No two bullets share the same opening verb, wording, or sentence structure. '
        'Each bullet describes a distinct responsibility, problem, or outcome and names exact technologies. '
        'Forbidden filler phrases: "Delivered production work across", "Collaborated with product and engineering stakeholders", '
        '"Strengthened reliability and delivery confidence", "Contributed as a ... in a fast-moving environment", '
        '"modern tools", "backend services", "cloud-based systems", "web technologies". '
        'Vary verbs: architect, own, design, ship, build, migrate, harden, instrument, refactor, mentor, lead, automate, optimize, integrate, debug, profile, partner, scale, reduce. '
        'STEP 6 — VALIDATION: '
        f'Before returning, verify: (a) each company has exactly its target bullet count, (b) skill_groups total {skills_min}–{skills_max} items, '
        '(c) no timeline violations. If any check fails, fix it before returning. '

        'Return only JSON matching the schema. Each work_history item references the source company by company_index (0-based) only — '
        'company_name, duration, and location are filled locally by the app.'
    )

    payload = {
        'job_description': job_description,
        'prompt_guidance': default_prompt,
        'profile': _profile_for_generation(profile),
        'bullet_targets_per_company': [
            {'index': idx, 'target_bullets': _bullet_target(idx)}
            for idx in range(work_history_count)
        ],
    }

    content, api_log = _openai_json_schema_call(
        client=client,
        model=model,
        developer_message=developer_message,
        payload=payload,
        schema_name='tailored_resume_v10',
        schema=RESUME_SCHEMA,
        flow_id=flow_id or _make_flow_id('resume_generate'),
        call_kind='generate_resume',
        attempt=attempt,
        user_id=profile_id,
    )

    return {
        'resume': _normalize_resume(content, profile=profile, target_role=target_role, job_description=job_description, job_tech_analysis=job_tech_analysis),
        'api_log': api_log,
    }


def _update_with_openai(profile: dict, job_description: str, current_resume: dict, fix_prompt: str, target_role: str, default_prompt: str, clean_generation: bool, flow_id: str = '', model: str = '') -> dict:
    from openai import OpenAI

    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
    model = model or os.getenv('OPENAI_MODEL', 'gpt-5.4')

    job_tech_analysis = _analyze_job_tech_stack(job_description, target_role=target_role)
    work_history_count = len(profile.get('work_history', []) or current_resume.get('work_history', []))
    gen_settings = profile.get('generation_settings') or {}
    bullet_counts = gen_settings.get('bullet_counts') or []

    def _bullet_target(idx: int) -> int:
        if idx < len(bullet_counts):
            try:
                return int(bullet_counts[idx])
            except Exception:
                pass
        return _target_bullet_count(idx, work_history_count)

    bullet_targets = ', '.join(
        f"company {idx + 1} (index {idx}) = exactly {_bullet_target(idx)} bullets"
        for idx in range(work_history_count)
    ) or 'each company 5 to 9 bullets'

    developer_message = (
        'You are revising an existing generated resume. Treat every request as isolated. '
        'Do not use any prior conversation context, prior drafts, or prior generations outside the current payload. '
        'Use only the current work history, job description, current resume draft, and the user fix request. '
        'Stay strictly truthful to the profile. Do not invent companies, dates, scope, responsibilities, metrics, certifications, or technologies not supported by the work history. '
        'Apply technology timeline rules: never mention AI/ML/LLMs in roles ending before 2021; never mention Kubernetes/GraphQL in roles ending before 2018; '
        'never mention Django/FastAPI/NestJS in roles ending before 2019; never mention Tailwind/Vite/Playwright in roles ending before 2020. '
        'Preserve the strengths of the current draft unless the fix request clearly asks to change them, but always apply the fix request precisely. '
        f'Use these per-company bullet targets when nothing in the fix request says otherwise: {bullet_targets}. '
        'Every bullet must be long enough to wrap to exactly 2 printed lines — never write a 1-line bullet. '
        'No two bullets across the whole resume may share the same opening verb or repeat the same wording. '
        'Avoid generic filler: "delivered production work", "collaborated with stakeholders", "strengthened reliability and delivery confidence", "contributed in a fast-moving environment". '
        'Return the full revised resume in the required JSON schema, not a partial patch.'
    )

    payload = {
        'job_description': job_description,
        'prompt_guidance': default_prompt,
        'profile': _profile_for_generation(profile),
        'current_resume': current_resume,
        'fix_prompt': fix_prompt,
        'bullet_targets_per_company': [
            {'index': idx, 'target_bullets': _bullet_target(idx)}
            for idx in range(work_history_count)
        ],
    }

    content, api_log = _openai_json_schema_call(
        client=client,
        model=model,
        developer_message=developer_message,
        payload=payload,
        schema_name='tailored_resume_update_v10',
        schema=RESUME_SCHEMA,
        flow_id=flow_id or _make_flow_id('resume_update'),
        call_kind='update_resume',
        attempt=1,
    )

    updated = _normalize_resume(content, profile=profile, target_role=target_role, job_description=job_description, job_tech_analysis=job_tech_analysis)
    for key in ('bold_keywords', 'auto_bold_fit_keywords'):
        if key in current_resume:
            updated[key] = current_resume[key]
    return {'resume': updated, 'api_log': api_log}


def _generate_demo_resume(profile: dict, job_description: str, target_role: str, default_prompt: str = '', clean_generation: bool = True, job_tech_analysis: dict | None = None) -> dict:
    effective_prompt = default_prompt
    analysis = job_tech_analysis or _analyze_job_tech_stack(job_description, target_role=target_role)
    extracted_keywords = analysis.get('keywords', [])
    expanded_stack = analysis.get('expanded_techs', [])
    profile_skills = _dedupe_preserve_order(profile.get('technical_skills', []))
    prioritized_skills = _prioritize_skills(expanded_stack or profile_skills, extracted_keywords)
    inferred_title = _infer_target_title(target_role, extracted_keywords, profile_skills + expanded_stack)
    headline = _infer_resume_headline(inferred_title, prioritized_skills)

    work_history = []
    profile_history = profile.get('work_history', [])
    total_jobs = len(profile_history)
    for idx, item in enumerate(profile_history):
        company_keywords = _keywords_for_company(item, extracted_keywords + expanded_stack[:18], profile_skills + expanded_stack) or expanded_stack[:6]
        role_title = _company_role_title(inferred_title, idx)
        role_headline = _build_role_headline(company_keywords, item.get('bullets', []), role_title)
        bullets = _dedupe_bullets(_tailored_bullets(item.get('bullets', []), company_keywords, prioritized_skills))
        target = _target_bullet_count(idx, total_jobs)
        if len(bullets) < target:
            fallback_pool = _dedupe_preserve_order(list(company_keywords) + list(prioritized_skills) + list(expanded_stack))[:9]
            extra = _fallback_bullets_for_role(role_title, fallback_pool or expanded_stack[:5], target - len(bullets), company_name=item.get('company_name', ''), index=idx)
            bullets = _dedupe_bullets(bullets + extra)
        work_history.append({
            'company_name': item.get('company_name', ''),
            'role_title': role_title,
            'role_headline': role_headline,
            'duration': item.get('duration', ''),
            'location': item.get('location', ''),
            'bullets': bullets[:9],
        })

    summary = _build_summary(profile, inferred_title, prioritized_skills[:10], effective_prompt)
    skill_groups = _group_skills_for_resume(prioritized_skills[:100], extracted_keywords)

    return _normalize_resume({
        'headline': headline,
        'summary': summary,
        'technical_skills': prioritized_skills[:100],
        'skill_groups': skill_groups,
        'fit_keywords': extracted_keywords[:18],
        'work_history': work_history,
        'education_history': profile.get('education_history', []),
    }, profile=profile, target_role=inferred_title, job_description=job_description)


def _update_demo_resume(profile: dict, job_description: str, current_resume: dict, fix_prompt: str, target_role: str) -> dict:
    updated = _normalize_resume(current_resume, profile=profile, target_role=target_role, job_description=job_description)
    analysis = _analyze_job_tech_stack(job_description, target_role=target_role)
    extracted_keywords = analysis.get('keywords', [])
    expanded_stack = analysis.get('expanded_techs', [])
    prioritized_skills = _prioritize_skills(expanded_stack or profile.get('technical_skills', []), extracted_keywords)
    updated['fit_keywords'] = _dedupe_preserve_order((updated.get('fit_keywords', []) or []) + extracted_keywords)[:18]
    updated['technical_skills'] = prioritized_skills[:100]
    updated['skill_groups'] = _group_skills_for_resume(updated.get('technical_skills', []), extracted_keywords)

    fix_lower = (fix_prompt or '').lower()
    inferred_title = _infer_target_title(target_role, extracted_keywords, profile.get('technical_skills', []) + expanded_stack)

    work_history = updated.get('work_history', [])
    total_jobs = len(work_history)
    for idx, job in enumerate(work_history):
        company_keywords = _keywords_for_company(job, extracted_keywords + expanded_stack[:18], profile.get('technical_skills', []) + expanded_stack) or prioritized_skills[:5]
        bullets = _dedupe_bullets(_tailored_bullets(job.get('bullets', []), company_keywords, prioritized_skills))
        target = _target_bullet_count(idx, total_jobs)
        if len(bullets) < target:
            fallback_pool = _dedupe_preserve_order(list(company_keywords) + list(prioritized_skills) + list(expanded_stack))[:9]
            extra = _fallback_bullets_for_role(job.get('role_title', inferred_title), fallback_pool, target - len(bullets), company_name=job.get('company_name', ''), index=idx)
            bullets = _dedupe_bullets(bullets + extra)
        job['bullets'] = bullets[:9]
        job['role_headline'] = _build_role_headline(company_keywords, job['bullets'], job.get('role_title', ''))

    if any(term in fix_lower for term in ['summary', 'headline', 'rewrite', 'sharper', 'tailor', 'tech', 'stack', 'keyword', 'specific', 'exact']):
        updated['headline'] = _infer_resume_headline(inferred_title, updated.get('technical_skills', []) or prioritized_skills)
        updated['summary'] = _build_summary(profile, inferred_title, updated.get('technical_skills', []) or prioritized_skills, fix_prompt)

    return _normalize_resume(updated, profile=profile, target_role=inferred_title, job_description=job_description)


def _generate_answers_with_openai(resume: dict, job_description: str, questions: list[str], target_role: str = '', flow_id: str = '', model: str = '') -> dict:
    from openai import OpenAI

    client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
    model = model or os.getenv('OPENAI_MODEL', 'gpt-5.4')

    developer_message = (
        'You write short, strong job application answers grounded only in the provided resume and job description. '
        'Sound like a real senior professional, not AI-generated copy. '
        'Write in first person. Keep each answer concise, usually 2 to 4 sentences. '
        'Be direct, credible, and specific. '
        'Do not invent experience, metrics, employers, technologies, or achievements that are not supported by the resume. '
        'Use named technologies and role-specific language where supported. '
        'Avoid clichés, exaggerated enthusiasm, filler, and robotic phrasing. '
        'Do not mention that you are tailoring or optimizing the answer. '
        'Return JSON only.'
    )

    payload = {
        'target_role': target_role,
        'job_description': job_description,
        'resume': resume,
        'questions': questions,
    }

    content, api_log = _openai_json_schema_call(
        client=client,
        model=model,
        developer_message=developer_message,
        payload=payload,
        schema_name='job_application_answers_v1',
        schema=APPLICATION_ANSWER_SCHEMA,
        flow_id=flow_id or _make_flow_id('application_answers'),
        call_kind='application_answers',
        attempt=1,
    )

    answers = [
        {
            'question': str(item.get('question', '')).strip(),
            'answer': str(item.get('answer', '')).strip(),
        }
        for item in content.get('answers', [])
        if str(item.get('question', '')).strip() and str(item.get('answer', '')).strip()
    ]
    return {'answers': answers, 'api_log': api_log}


def _generate_demo_answers(resume: dict, job_description: str, questions: list[str], target_role: str = '') -> list[dict]:
    keywords = _extract_keywords(job_description)
    fit_keywords = _dedupe_preserve_order((resume or {}).get('fit_keywords', []) + keywords)
    headline = str((resume or {}).get('headline', '')).strip()
    work_history = (resume or {}).get('work_history', []) or []
    skills = (resume or {}).get('technical_skills', []) or []
    lead_skills = ', '.join(skills[:4])
    recent_role = work_history[0].get('role_title', '') if work_history else ''
    recent_company = work_history[0].get('company_name', '') if work_history else ''
    recent_bullets = work_history[0].get('bullets', []) if work_history else []
    first_bullet = recent_bullets[0].strip().rstrip('.') if recent_bullets else ''
    target = target_role or headline or _infer_target_title('', keywords, skills)

    results: list[dict] = []
    for question in questions:
        q = question.lower()
        if 'why' in q and ('fit' in q or 'qualified' in q or 'good' in q or 'match' in q):
            answer = (
                f'I am a strong fit because my background lines up well with the core focus of this role, especially around {lead_skills or ", ".join(fit_keywords[:3])}. '
                f'Most of my recent work has been in roles like {recent_role or target}, where I have been responsible for production delivery, system reliability, and shipping features that map closely to this job.'
            )
        elif 'why' in q and ('want' in q or 'interested' in q):
            answer = (
                f'I am interested in this role because it lines up with the kind of work I have been doing and want to keep growing in, especially around {", ".join(fit_keywords[:3]) or target}. '
                f'I like roles where I can own delivery end to end, work closely with product and engineering, and bring strong execution in production environments.'
            )
        elif 'tell me about yourself' in q or ('introduce' in q and 'yourself' in q):
            answer = (
                f'I am a senior engineer with experience across {lead_skills or target}, and my work has been focused on building reliable production systems that are closely tied to business needs. '
                f'Recently, I have been working in positions like {recent_role or target} at {recent_company or "product-focused teams"}, where I have owned delivery from implementation through rollout and iteration.'
            )
        elif 'experience' in q or 'background' in q:
            answer = (
                f'My background is strongest in {lead_skills or ", ".join(fit_keywords[:4])}, with hands-on work across production applications, APIs, and role-aligned engineering problems. '
                f'A good example is my recent work at {recent_company or "my current team"}, where {first_bullet or "I handled end-to-end delivery of production-facing work and collaborated closely with cross-functional stakeholders"}.'
            )
        elif 'challenge' in q or 'difficult' in q or 'problem' in q:
            answer = (
                f'One pattern I have dealt with repeatedly is balancing delivery speed with maintainability in production systems. '
                f'My approach is to narrow the problem first, ship the smallest safe improvement, and make sure the implementation is supported by clear ownership, testing, and the right technical choices such as {lead_skills or ", ".join(fit_keywords[:3])} when they fit the problem.'
            )
        else:
            answer = (
                f'My experience is a strong match for this role because I have worked on production systems using {lead_skills or ", ".join(fit_keywords[:4])}, and I tend to be most effective in roles where I can combine hands-on implementation with strong ownership. '
                f'That is the kind of contribution I would bring here as well.'
            )
        results.append({'question': question, 'answer': ' '.join(answer.split())})
    return results


def _profile_for_generation(profile: dict) -> dict:
    """Send only what the AI needs to write content — no PII, no metadata."""
    total_years = profile.get('total_years_of_experience')
    result: dict = {
        'work_history': [
            {
                'index': idx,
                'duration': item.get('duration', ''),
                'seed_bullets': item.get('bullets', []),
            }
            for idx, item in enumerate(profile.get('work_history', []))
        ],
    }
    if total_years:
        result['total_years_of_experience'] = int(total_years)
    return result



def _normalize_resume(resume: dict, profile: dict, target_role: str, job_description: str, job_tech_analysis: dict | None = None) -> dict:
    source_history = profile.get('work_history', [])
    generated_items = resume.get('work_history') or []
    tech_analysis = job_tech_analysis or _analyze_job_tech_stack(job_description, target_role=target_role)
    extracted = tech_analysis.get('keywords', [])
    expanded_stack = tech_analysis.get('expanded_techs', [])
    inferred_title = _infer_target_title(target_role, extracted, profile.get('technical_skills', []) + expanded_stack)
    gen_settings = profile.get('generation_settings') or {}
    profile_bullet_counts = gen_settings.get('bullet_counts') or []

    generated_by_index: dict[int, dict] = {}
    ordered_generated: list[dict] = []
    for idx, item in enumerate(generated_items):
        if not isinstance(item, dict):
            continue
        company_index = item.get('company_index')
        try:
            company_index = int(company_index)
        except Exception:
            company_index = idx
        if company_index < 0:
            company_index = idx
        clean_item = {
            'company_index': company_index,
            'role_title': str(item.get('role_title', '')).strip(),
            'bullet_count': item.get('bullet_count'),
            'bullets': [str(b).strip() for b in item.get('bullets', []) if str(b).strip()],
        }
        generated_by_index[company_index] = clean_item
        ordered_generated.append(clean_item)

    normalized_history = []
    total_jobs = len(source_history)
    seen_bullet_keys: set[str] = set()
    for index, source_job in enumerate(source_history):
        item = generated_by_index.get(index) or (ordered_generated[index] if index < len(ordered_generated) else {})
        bullets = _dedupe_bullets(item.get('bullets', source_job.get('bullets', [])))
        bullets = [b for b in bullets if b.lower().rstrip('.') not in seen_bullet_keys]
        if index < len(profile_bullet_counts):
            try:
                target = int(profile_bullet_counts[index])
            except Exception:
                target = _target_bullet_count(index, total_jobs)
        else:
            target = _target_bullet_count(index, total_jobs)
        company_keywords = _keywords_for_company(source_job, extracted + expanded_stack[:18], profile.get('technical_skills', []) + expanded_stack) or expanded_stack[:5]
        if len(bullets) < target:
            fallback_pool = _dedupe_preserve_order(list(company_keywords) + list(expanded_stack) + list(profile.get('technical_skills', [])))[:9]
            extra = _fallback_bullets_for_role(item.get('role_title') or _company_role_title(inferred_title, index), fallback_pool, target - len(bullets), company_name=source_job.get('company_name', ''), index=index)
            extra = [b for b in extra if b.lower().rstrip('.') not in seen_bullet_keys and b.lower().rstrip('.') not in {x.lower().rstrip('.') for x in bullets}]
            bullets = _dedupe_bullets(bullets + extra)
        bullets = bullets[:target]
        bullets = [_enforce_bullet_length(b) for b in bullets]
        for bullet in bullets:
            seen_bullet_keys.add(bullet.lower().rstrip('.'))
        normalized_history.append({
            'company_name': source_job.get('company_name', ''),
            'role_title': item.get('role_title') or _company_role_title(inferred_title, index),
            'role_headline': '',
            'duration': source_job.get('duration', ''),
            'location': source_job.get('location', ''),
            'bullets': bullets,
        })

    ai_groups = _normalize_skill_groups(resume.get('skill_groups', []))
    skills_count = gen_settings.get('skills_count') or 85
    skills_min = max(80, skills_count - 5)
    skills_max = skills_count + 5

    import random as _random
    profile_id = str(profile.get('id') or '').strip()
    # Per-profile seed: shuffle items within categories so different profiles
    # get different orderings even when the AI returns identical category lists.
    _rng = _random.Random(profile_id or None)

    if ai_groups:
        # Trust AI's groups directly — accept any string, no static filter
        seen_items: set[str] = set()
        all_ai_items: list[str] = []
        for group in ai_groups:
            for item in group.get('items', []):
                key = item.strip().lower()
                if key and key not in seen_items:
                    seen_items.add(key)
                    all_ai_items.append(item.strip())

        # Pad each category to at least 10 using items from other AI groups (not static pool)
        padded_groups: list[dict] = []
        used_for_padding: set[str] = set()
        for group in ai_groups:
            items = [i.strip() for i in group.get('items', []) if i.strip()]
            items = list(dict.fromkeys(items))  # dedupe within category
            # Shuffle within the category using profile-specific seed
            _rng.shuffle(items)
            used_for_padding.update(i.lower() for i in items)
            padded_groups.append({'category': group['category'], 'items': items})

        # If any category is under 10, pull from other AI categories
        extra_pool = [i for i in all_ai_items if i.lower() not in used_for_padding]
        for group in padded_groups:
            if len(group['items']) < 10:
                needed = 10 - len(group['items'])
                for extra in list(extra_pool):
                    if extra.lower() not in {i.lower() for i in group['items']}:
                        group['items'].append(extra)
                        extra_pool.remove(extra)
                        needed -= 1
                        if needed == 0:
                            break

        # Flatten to flat skills list (preserving group order)
        technical_skills_flat: list[str] = []
        flat_seen: set[str] = set()
        for group in padded_groups:
            for item in group['items']:
                key = item.lower()
                if key not in flat_seen:
                    flat_seen.add(key)
                    technical_skills_flat.append(item)

        # Pad to skills_min from expanded_stack if still short
        if len(technical_skills_flat) < skills_min:
            for tech in (expanded_stack or []) + KNOWN_TECH_TERMS:
                if tech.lower() not in flat_seen:
                    flat_seen.add(tech.lower())
                    technical_skills_flat.append(tech)
                    # Add to the last group as a catch-all
                    if padded_groups:
                        padded_groups[-1]['items'].append(tech)
                if len(technical_skills_flat) >= skills_min:
                    break

        technical_skills = technical_skills_flat[:skills_max]
        generated_groups = padded_groups
    else:
        # No AI groups: fall back to static rebuild
        grouped_input_items = _ensure_tech_range(
            _dedupe_preserve_order([i for g in [] for i in g.get('items', [])]),
            expanded_stack, minimum=skills_min, maximum=skills_max
        )
        technical_skills = grouped_input_items
        generated_groups = _group_skills_for_resume(technical_skills, extracted)

    summary = resume.get('summary') or _build_summary(profile, inferred_title, technical_skills, '')
    normalized = {
        'headline': resume.get('headline') or _infer_resume_headline(inferred_title, technical_skills),
        'summary': summary,
        'technical_skills': technical_skills,
        'skill_groups': generated_groups,
        'fit_keywords': extracted[:18],
        'work_history': normalized_history,
        'education_history': profile.get('education_history', []),
    }
    for optional_key in ('bold_keywords', 'auto_bold_fit_keywords'):
        if optional_key in resume:
            normalized[optional_key] = resume[optional_key]
    return normalized


def _build_summary(profile: dict, target_title: str, prioritized_skills: list[str], prompt_guidance: str) -> str:
    seed = profile.get('summary_seed', '').strip()
    tech_focus = ', '.join(prioritized_skills[:7])
    first = f'{target_title} with strong experience building production-grade systems' if target_title else 'Engineer with strong experience building production-grade systems'
    if tech_focus:
        first += f' across {tech_focus}'
    first += '.'

    lines = [first]
    if seed:
        seed_clean = seed.rstrip()
        if seed_clean and seed_clean[-1] not in '.!?':
            seed_clean += '.'
        lines.append(seed_clean)
    if prompt_guidance.strip():
        lines.append('The resume is tuned to the current role with a focused technical stack and role-aligned execution history.')
    return ' '.join(lines).strip()



def _keywords_for_company(job: dict, extracted_keywords: list[str], profile_skills: list[str]) -> list[str]:
    corpus = ' '.join([job.get('company_name', ''), job.get('location', ''), *job.get('bullets', [])]).lower()
    matched = [kw for kw in extracted_keywords if kw.lower() in corpus]
    if matched:
        return matched[:5]
    tech_in_bullets = []
    for skill in profile_skills:
        if skill.lower() in corpus and skill not in tech_in_bullets:
            tech_in_bullets.append(skill)
    return tech_in_bullets[:5]



def _company_role_title(base_title: str, index: int) -> str:
    title = base_title or 'Software Engineer'
    if index == 0 and not re.search(r'\b(Senior|Lead|Staff|Principal)\b', title, flags=re.I):
        return f'Senior {title}'.replace('Senior Senior', 'Senior').strip()
    if index >= 2 and re.search(r'\bSenior\b', title, flags=re.I):
        cleaned = re.sub(r'\bSenior\b\s*', '', title, flags=re.I).strip()
        return cleaned or title
    return title



def _build_role_headline(keywords: list[str], bullets: list[str], role_title: str) -> str:
    if keywords:
        return f'Built production-facing work across {", ".join(keywords[:4])} in a role aligned to {role_title}.'
    if bullets:
        phrase = bullets[0].strip().rstrip('.')
        return (phrase[:115] + '...') if len(phrase) > 118 else phrase
    return f'Tailored experience positioning for {role_title}.'



def _tailored_bullets(bullets: list[str], company_keywords: list[str], prioritized_skills: list[str]) -> list[str]:
    if not bullets:
        return []
    chosen = company_keywords or prioritized_skills[:3]
    rewritten = []
    for idx, bullet in enumerate(bullets):
        clean = ' '.join(str(bullet).strip().split()).rstrip('.')
        if not clean:
            continue
        if _contains_named_tech(clean, chosen):
            rewritten.append(clean + '.')
            continue
        if chosen:
            tech_phrase = ', '.join(chosen[:3])
            if idx == 0:
                rewritten.append(f'{clean} using {tech_phrase}.')
            else:
                rewritten.append(f'{clean}, with hands-on work in {tech_phrase}.')
        else:
            rewritten.append(clean + '.')
    return rewritten



def _contains_named_tech(text: str, techs: Iterable[str]) -> bool:
    lower = text.lower()
    return any(str(skill).lower() in lower for skill in techs if str(skill).strip())



def _infer_target_title(target_role: str, keywords: list[str], profile_skills: list[str]) -> str:
    if str(target_role).strip():
        return ' '.join(str(target_role).split())
    blob = ' '.join([*keywords, *profile_skills]).lower()
    best_title = 'Software Engineer'
    best_score = -1
    for title, hints in ROLE_HINTS:
        score = sum(2 if hint in blob else 0 for hint in hints)
        if title.lower() in blob:
            score += 3
        if score > best_score:
            best_score = score
            best_title = title
    return best_title



def _infer_resume_headline(title: str, prioritized_skills: list[str]) -> str:
    focus = [skill for skill in prioritized_skills[:4] if skill]
    if focus:
        return f"{title} | {' | '.join(focus[:3])}"
    return title or 'Professional Engineer'



def _extract_keywords(job_description: str) -> list[str]:
    analysis = _analyze_job_tech_stack(job_description, target_role='')
    base = analysis.get('explicit_techs', []) + analysis.get('expanded_techs', [])[:18]

    text = job_description or ''
    raw_candidates = re.findall(r'[A-Za-z][A-Za-z0-9+.#/-]{2,}', text)
    stop_words = {
        'the', 'and', 'with', 'for', 'this', 'that', 'will', 'have', 'your', 'from', 'into', 'about', 'years',
        'experience', 'team', 'build', 'building', 'role', 'work', 'using', 'developer', 'engineer', 'software',
        'strong', 'plus', 'across', 'are', 'must', 'you', 'our', 'web', 'full', 'stack', 'features', 'ability',
        'required', 'preferred', 'nice', 'looking', 'seeking', 'position', 'candidate', 'need', 'needs', 'including', 'responsible', 'responsibilities', 'opportunity', 'environment'
    }
    counts: Counter[str] = Counter()
    for token in raw_candidates:
        clean = token.strip('.,:;()[]{}')
        if clean.lower() in stop_words:
            continue
        counts[clean] += 1

    keywords = _dedupe_preserve_order(base)
    for token, _ in counts.most_common(20):
        canonical = _canonical_term(token)
        if canonical not in keywords:
            keywords.append(canonical)
    return keywords[:24]



def _prioritize_skills(profile_skills: list[str], jd_keywords: list[str]) -> list[str]:
    deduped_profile = [skill for skill in _dedupe_preserve_order(profile_skills) if _is_technical_stack_item(skill)]
    if not deduped_profile:
        deduped_profile = [skill for skill in _dedupe_preserve_order(jd_keywords) if _is_technical_stack_item(skill)]
    jd_lookup = {_canonical_term(item).lower() for item in jd_keywords}
    exact_matches = [skill for skill in deduped_profile if _canonical_term(skill).lower() in jd_lookup]
    near_matches = []
    for skill in deduped_profile:
        skill_key = _canonical_term(skill).lower()
        if skill in exact_matches:
            continue
        if any(skill_key in kw.lower() or kw.lower() in skill_key for kw in jd_keywords):
            near_matches.append(skill)
    tail = [skill for skill in deduped_profile if skill not in exact_matches and skill not in near_matches]
    return exact_matches + near_matches + tail



_CATEGORY_MIN_ITEMS = 10

def _group_skills_for_resume(skills: list[str], keywords: list[str]) -> list[dict]:
    deduped_skills = [skill for skill in _dedupe_preserve_order(skills) if _is_technical_stack_item(skill)]
    normalized_lookup = {_canonical_term(skill).lower(): skill for skill in deduped_skills}
    grouped: list[dict] = []
    used: set[str] = set()

    preferred_order = list(CATEGORY_ALIASES.keys())
    keyword_blob = ' '.join(keyword.lower() for keyword in keywords)
    preferred_order.sort(key=lambda category: sum(alias.lower() in keyword_blob for alias in CATEGORY_ALIASES[category]), reverse=True)

    for category in preferred_order:
        matches: list[str] = []
        for alias in CATEGORY_ALIASES[category]:
            key = _canonical_term(alias).lower()
            if key in normalized_lookup and normalized_lookup[key] not in used:
                matches.append(normalized_lookup[key])
                used.add(normalized_lookup[key])
        if matches:
            # Pad to minimum using the category's alias pool
            if len(matches) < _CATEGORY_MIN_ITEMS:
                for alias in CATEGORY_ALIASES[category]:
                    if len(matches) >= _CATEGORY_MIN_ITEMS:
                        break
                    if alias not in used and alias not in matches:
                        matches.append(alias)
                        used.add(alias)
            grouped.append({'category': category, 'items': matches})

    extras = [skill for skill in deduped_skills if skill not in used]
    if extras:
        # Try to pad existing small groups with extras before adding Other Relevant
        for group in grouped:
            if len(group['items']) < _CATEGORY_MIN_ITEMS and extras:
                needed = _CATEGORY_MIN_ITEMS - len(group['items'])
                group['items'].extend(extras[:needed])
                used.update(extras[:needed])
                extras = extras[needed:]
        remaining = [s for s in extras if s not in used]
        if remaining:
            grouped.append({'category': 'Other Relevant', 'items': remaining})
    return grouped



def _normalize_skill_groups(groups: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for group in groups or []:
        category = str(group.get('category', '')).strip()
        items = _dedupe_preserve_order(group.get('items', []))
        if category and items:
            normalized.append({'category': category, 'items': items})
    return normalized



def _compose_prompt_guidance(default_prompt: str, custom_prompt: str) -> str:
    return '\n\n'.join(part.strip() for part in [default_prompt, custom_prompt] if str(part).strip())



def _canonical_term(value: str) -> str:
    clean = ' '.join(str(value).split()).strip()
    if not clean:
        return ''
    return TECH_ALIAS_MAP.get(clean.lower(), clean)



_KNOWN_TECH_LOWER: set[str] = {t.lower() for t in KNOWN_TECH_TERMS}

def _is_technical_stack_item(value: str) -> bool:
    canonical = _canonical_term(value)
    if canonical in KNOWN_TECH_TERMS:
        return True
    # Also accept anything whose canonical form is in the known set (case-insensitive)
    return canonical.lower() in _KNOWN_TECH_LOWER


def _extract_explicit_jd_techs(job_description: str) -> list[str]:
    text = job_description or ''
    lowered = text.lower()
    found: list[str] = []
    for alias, canonical in TECH_ALIAS_MAP.items():
        if alias in lowered and canonical not in found:
            found.append(canonical)
    for tech in KNOWN_TECH_TERMS:
        if tech.lower() in lowered and tech not in found:
            found.append(tech)
    return found


def _role_stack_family(target_role: str, job_description: str) -> str:
    return _infer_target_title(target_role, _extract_explicit_jd_techs(job_description), [])


def _ensure_tech_range(existing: list[str], required_stack: list[str], minimum: int = 40, maximum: int = 50) -> list[str]:
    combined = [skill for skill in _dedupe_preserve_order(existing + required_stack) if _is_technical_stack_item(skill)]
    if len(combined) < minimum:
        for family in ROLE_TECH_STACKS.values():
            for tech in family:
                if tech not in combined and _is_technical_stack_item(tech):
                    combined.append(tech)
                if len(combined) >= minimum:
                    break
            if len(combined) >= minimum:
                break
    # Final fallback: pad from KNOWN_TECH_TERMS (covers languages + professional skills)
    if len(combined) < minimum:
        for tech in KNOWN_TECH_TERMS:
            if tech not in combined:
                combined.append(tech)
            if len(combined) >= minimum:
                break
    return combined[:maximum]


def _analyze_job_tech_stack(job_description: str, target_role: str = '') -> dict:
    explicit_techs = _extract_explicit_jd_techs(job_description)
    keywords = _dedupe_preserve_order(explicit_techs + _extract_raw_jd_terms(job_description))
    role_family = _role_stack_family(target_role, job_description)
    expanded_techs = _expand_related_techs(explicit_techs, role_family, keywords, minimum=60, maximum=70)
    if len(expanded_techs) < 40:
        expanded_techs = _expand_related_techs(expanded_techs + explicit_techs, role_family, keywords, minimum=60, maximum=70, second_pass=True)
    expanded_techs = _ensure_tech_range(expanded_techs, ROLE_TECH_STACKS.get(role_family, []), minimum=60, maximum=70)
    return {
        'explicit_techs': explicit_techs,
        'expanded_techs': expanded_techs,
        'keywords': keywords,
        'role_family': role_family,
    }


def _extract_raw_jd_terms(job_description: str) -> list[str]:
    text = job_description or ''
    raw_candidates = re.findall(r'[A-Za-z][A-Za-z0-9+.#/-]{2,}', text)
    stop_words = {
        'the', 'and', 'with', 'for', 'this', 'that', 'will', 'have', 'your', 'from', 'into', 'about', 'years', 'experience', 'team',
        'build', 'building', 'role', 'work', 'using', 'developer', 'engineer', 'software', 'strong', 'plus', 'across', 'are', 'must',
        'you', 'our', 'web', 'full', 'stack', 'features', 'ability', 'required', 'preferred', 'nice', 'looking', 'seeking', 'position',
        'candidate', 'need', 'needs', 'including', 'responsible', 'responsibilities', 'opportunity', 'environment'
    }
    ordered: list[str] = []
    for token in raw_candidates:
        clean = token.strip('.,:;()[]{}')
        canonical = _canonical_term(clean)
        if clean.lower() in stop_words:
            continue
        if canonical not in ordered:
            ordered.append(canonical)
    return ordered[:30]


def _expand_related_techs(seed_techs: list[str], role_family: str, keywords: list[str], minimum: int = 40, maximum: int = 50, second_pass: bool = False) -> list[str]:
    ordered = [tech for tech in _dedupe_preserve_order(seed_techs) if _is_technical_stack_item(tech)]

    for tech in list(ordered):
        for related in TECH_EXPANSION_MAP.get(tech, []):
            if related not in ordered and _is_technical_stack_item(related):
                ordered.append(related)

    for keyword in keywords:
        canonical = _canonical_term(keyword)
        if canonical in TECH_EXPANSION_MAP:
            for related in TECH_EXPANSION_MAP.get(canonical, []):
                if related not in ordered and _is_technical_stack_item(related):
                    ordered.append(related)

    for tech in ROLE_TECH_STACKS.get(role_family, []):
        if tech not in ordered and _is_technical_stack_item(tech):
            ordered.append(tech)
        if len(ordered) >= minimum:
            break

    if second_pass or len(ordered) < minimum:
        for family_name, family_stack in ROLE_TECH_STACKS.items():
            if family_name == role_family:
                continue
            for tech in family_stack:
                if tech not in ordered and _is_technical_stack_item(tech):
                    ordered.append(tech)
                if len(ordered) >= minimum:
                    break
            if len(ordered) >= minimum:
                break

    for tech in KNOWN_TECH_TERMS:
        if tech not in ordered:
            ordered.append(tech)
        if len(ordered) >= minimum:
            break

    return ordered[:maximum]


def _target_bullet_count(index: int, total: int) -> int:
    """Return how many bullets a company at the given index should carry.

    Company 1 (latest): 14-15 bullets to fill page 1 completely.
    Companies 2 & 3: 10-11 bullets each to fill page 2 completely.
    Company 4+: 7-8 bullets.
    """
    if total <= 0:
        return 7
    if index == 0:
        return 14
    if index == 1:
        return 10
    if index == 2:
        return 10
    return 8


_BULLET_MIN_CHARS = 120
_BULLET_MAX_CHARS = 175


def _enforce_bullet_length(bullet: str) -> str:
    """Truncate bullets that exceed 175 chars at the nearest word boundary.

    Bullets under 120 chars are left as-is (the AI should have avoided them,
    but truncating wouldn't help — the content just stays short).
    Strips markdown bold markers (**text**) from the raw string for length
    measurement, then truncates the original preserving any markers.
    """
    # Strip markdown bold for length check only
    import re as _re
    clean = _re.sub(r'\*\*([^*]+)\*\*', r'\1', bullet)
    if len(clean) <= _BULLET_MAX_CHARS:
        return bullet
    # Truncate at word boundary within the original string to stay under limit
    # Walk backwards from position 175 (in clean) to find a space
    truncate_at = _BULLET_MAX_CHARS
    while truncate_at > _BULLET_MIN_CHARS and truncate_at < len(clean) and clean[truncate_at] != ' ':
        truncate_at -= 1
    # Map clean-string position back to original (bold markers add chars)
    # Simple approach: truncate original at equivalent word boundary
    words = bullet.split()
    result = ''
    for word in words:
        candidate = (result + ' ' + word).lstrip() if result else word
        candidate_clean = _re.sub(r'\*\*([^*]+)\*\*', r'\1', candidate)
        if len(candidate_clean) > _BULLET_MAX_CHARS:
            break
        result = candidate
    return result.rstrip('.,;:') + '.' if result else bullet[:_BULLET_MAX_CHARS].rstrip() + '.'


def _dedupe_bullets(bullets: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for bullet in bullets or []:
        clean = ' '.join(str(bullet).split()).strip()
        if not clean:
            continue
        key = clean.lower().rstrip('.')
        if key in seen:
            continue
        seen.add(key)
        unique.append(clean)
    return unique


def _fallback_bullets_for_role(role_title: str, techs: list[str], needed: int, company_name: str = '', index: int = 0) -> list[str]:
    if needed <= 0:
        return []
    raw = [str(t).strip() for t in (techs or []) if str(t).strip()]
    # Dedupe while preserving order so adjacent template slots cannot read
    # like "IAM and IAM".
    seen: set[str] = set()
    techs_clean: list[str] = []
    for tech in raw:
        key = tech.lower()
        if key in seen:
            continue
        seen.add(key)
        techs_clean.append(tech)
    if not techs_clean:
        techs_clean = ['production tooling']
    # Rotate the tech list by company index so each company leads with a
    # different stack flavor; this multiplies bullet diversity even when the
    # template bank repeats.
    if len(techs_clean) > 1:
        offset = index % len(techs_clean)
        techs_clean = techs_clean[offset:] + techs_clean[:offset]
    primary = ', '.join(techs_clean[:3])
    secondary = ', '.join(techs_clean[3:6]) if len(techs_clean) > 3 else primary
    tertiary = ', '.join(techs_clean[6:9]) if len(techs_clean) > 6 else secondary
    lead = techs_clean[0]
    second = techs_clean[1] if len(techs_clean) > 1 else lead
    third = techs_clean[2] if len(techs_clean) > 2 else second
    role = role_title.strip() or 'Software Engineer'
    role_lower = role.lower()
    company = company_name.strip() or 'the team'

    bank = [
        f'Owned {role_lower} delivery on {primary}, partnering with product to ship features end to end.',
        f'Designed and shipped {lead}-driven services with tested {second} integrations and {third} support across the {company} stack.',
        f'Operated production workloads on {secondary}, hardening reliability with monitoring, alerting, and disciplined rollouts.',
        f'Built CI/CD and developer tooling around {tertiary} to shorten lead time and de-risk releases.',
        f'Refactored {lead} integration paths and grew test coverage on {second}, cutting regressions across release cycles.',
        f'Mentored teammates on {primary} patterns through code reviews, design docs, and pairing on tougher delivery slices.',
        f'Investigated and resolved production incidents touching {secondary}, capturing learnings into playbooks and dashboards.',
        f'Drove cross-team alignment on {primary} architecture, balancing delivery speed with long-term maintainability.',
        f'Led performance work on {lead} and {second}, profiling hot paths and rolling out throughput-sensitive optimizations.',
        f'Migrated legacy components onto {primary}, preserving behavior with characterization tests and gradual cutovers.',
        f'Hardened security posture for {lead} and {third} surfaces with secrets management, IAM tightening, and automated scans.',
        f'Instrumented {primary} with structured logging and metrics so on-call could triage issues without paging the owning team.',
        f'Partnered with data and platform teams to standardize {secondary} interfaces, removing one-off integrations and review churn.',
        f'Championed code-quality and review standards across {lead} and {second}, reducing rework on shipped changes.',
        f'Automated repetitive {tertiary} workflows with scripts and pipelines, freeing engineering time for higher-leverage work.',
        f'Scaled {lead} throughput by tuning {second} configuration, batching strategies, and back-pressure handling.',
        f'Wrote durable runbooks for {primary} so new engineers could ship and operate confidently from day one.',
        f'Reviewed and approved high-impact changes touching {secondary}, defending product quality without slowing the team.',
        f'Built end-to-end tests around {lead} and {third} flows, catching regressions before they reached customers.',
        f'Owned upgrades for {lead} and {second} dependencies, sequencing breaking changes safely across services.',
        f'Cut cloud spend on {primary} by right-sizing resources, enforcing budgets, and removing unused infrastructure.',
        f'Co-designed APIs across {lead} and {third} so partner teams could integrate without ad-hoc workarounds.',
    ]
    # Step coprime to len(bank) (22) so rotation keeps each company's window
    # genuinely different from the others.
    step = 7
    rotation = (index * step) % len(bank)
    rotated = bank[rotation:] + bank[:rotation]
    return rotated[:needed]


def _resume_meets_generation_requirements(resume: dict, job_tech_analysis: dict, bullet_counts: list | None = None) -> dict:
    required_stack = job_tech_analysis.get('expanded_techs', []) or []
    technical_skills = [skill for skill in _dedupe_preserve_order(resume.get('technical_skills', [])) if _is_technical_stack_item(skill)]
    grouped_items = [item for group in resume.get('skill_groups', []) or [] for item in group.get('items', [])]
    technical_skills = _dedupe_preserve_order(technical_skills + [item for item in grouped_items if _is_technical_stack_item(item)])
    actual_lookup = {skill.lower() for skill in technical_skills}
    missing_required_techs = [skill for skill in required_stack if skill.lower() not in actual_lookup]

    work_history = resume.get('work_history', []) or []
    total_jobs = len(work_history)
    bullet_gaps: list[str] = []
    total_bullets = 0
    tech_bullets = 0
    seen_bullet_keys: set[str] = set()
    duplicate_bullets = 0
    for idx, job in enumerate(work_history):
        bullets = [str(b).strip() for b in job.get('bullets', []) if str(b).strip()]
        total_bullets += len(bullets)
        if bullet_counts and idx < len(bullet_counts):
            try:
                target = int(bullet_counts[idx])
            except Exception:
                target = _target_bullet_count(idx, total_jobs)
        else:
            target = _target_bullet_count(idx, total_jobs)
        if len(bullets) < target:
            bullet_gaps.append(f"Company index {idx} has {len(bullets)} bullets; expected at least {target}.")
        for bullet in bullets:
            lower = bullet.lower()
            key = lower.rstrip('.')
            if key in seen_bullet_keys:
                duplicate_bullets += 1
            seen_bullet_keys.add(key)
            if any(skill.lower() in lower for skill in required_stack[:24]) or any(skill.lower() in lower for skill in KNOWN_TECH_TERMS):
                tech_bullets += 1
    bullet_ratio = (tech_bullets / total_bullets) if total_bullets else 0.0
    if total_bullets == 0:
        bullet_gaps.append('No bullets were returned in work history.')
    elif bullet_ratio < 0.65:
        bullet_gaps.append('Too many bullets still use generic wording instead of named technologies.')
    if duplicate_bullets > 0:
        bullet_gaps.append(f'{duplicate_bullets} bullet(s) duplicate wording across companies; rewrite each to be unique.')

    ok = (
        80 <= len(technical_skills) <= 100
        and not bullet_gaps
        and len(missing_required_techs) <= 8
        and duplicate_bullets == 0
    )
    return {
        'ok': ok,
        'skills_count': len(technical_skills),
        'missing_required_techs': missing_required_techs,
        'bullet_gaps': bullet_gaps,
        'bullet_ratio': round(bullet_ratio, 2),
        'duplicate_bullets': duplicate_bullets,
    }


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        clean = str(item).strip()
        if not clean:
            continue
        canonical = _canonical_term(clean)
        key = canonical.lower()
        if key not in seen:
            seen.add(key)
            ordered.append(canonical)
    return ordered
