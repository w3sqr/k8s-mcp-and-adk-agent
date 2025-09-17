from google.adk.agents import LlmAgent
import os
import requests
import time
import json
import logging
from typing import Any, List, Dict

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("mcp_agent")

# Helper to call MCP server tool proxy
MCP_SERVICE_URL = os.environ.get('MCP_SERVICE_URL', 'http://k8s-mcp-service.default.svc.cluster.local:8080')

def call_mcp_tool(tool_name: str, args: list = None, kwargs: dict = None) -> Any:
    """Call MCP server tool with better error handling and logging"""
    url = f"{MCP_SERVICE_URL}/tool/{tool_name}"
    payload = {
        'args': args or [],
        'kwargs': kwargs or {}
    }
    
    logger.info(f"Calling MCP tool: {tool_name} with args={args}, kwargs={kwargs}")
    
    # Try a couple of times for transient connection issues
    last_exc = None
    for attempt in range(1, 4):
        try:
            resp = requests.post(url, json=payload, timeout=30)
            logger.info(f"MCP tool {tool_name} response status: {resp.status_code}")
            
            # If non-JSON response or non-2xx, surface status + body for debugging
            try:
                data = resp.json()
                logger.info(f"MCP tool {tool_name} response data: {data}")
            except Exception:
                resp_body = resp.text
                logger.warning(f"Non-JSON response from {tool_name}: {resp_body}")
                resp.raise_for_status()
                return resp_body

            if resp.status_code >= 400:
                # Expect server returns structured {ok: False, error: '...'}
                err = data.get('error') if isinstance(data, dict) else resp.text
                return f"MCP server returned error {resp.status_code}: {err}"

            if data.get('ok'):
                result = data.get('result')
                logger.info(f"MCP tool {tool_name} succeeded with result: {str(result)[:200]}...")
                return result
            else:
                error_msg = f"MCP tool error: {data.get('error', 'Unknown error')}"
                logger.error(error_msg)
                return error_msg

        except requests.exceptions.RequestException as e:
            last_exc = e
            logger.warning(f"Attempt {attempt} failed for tool {tool_name}: {e}")
            # brief backoff
            time_sleep = 1 * attempt
            try:
                time.sleep(time_sleep)
            except Exception:
                pass
            continue
        except Exception as e:
            logger.error(f"Unexpected error calling tool {tool_name}: {e}")
            return f"Unexpected error calling MCP tool {tool_name}: {e}"

    error_msg = f"Failed to call MCP tool {tool_name} after 3 attempts: {last_exc}"
    logger.error(error_msg)
    return error_msg


# Wrapper tools for ADK agent
def discover_tools() -> list:
    """Discover available tools from MCP server"""
    try:
        resp = requests.get(f"{MCP_SERVICE_URL}/tools", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        tools = data.get('tools', [])
        logger.info(f"Discovered MCP tools: {tools}")
        return tools
    except Exception as e:
        logger.error(f"Failed to discover tools: {e}")
        return []


def make_tool_wrapper(tool_name: str):
    """Create a wrapper function for an MCP tool with improved argument handling"""
    
    if tool_name == "list_pods":
        def list_pods_wrapper(namespace: str = "default", show_all: str = "false"):
            """List pods in a specific namespace with their status."""
            return call_mcp_tool(tool_name, kwargs={'namespace': namespace, 'show_all': show_all})
        
        list_pods_wrapper.__name__ = f"mcp_tool_{tool_name}"
        list_pods_wrapper.__doc__ = f"List pods in a namespace. Args: namespace (str, default='default'), show_all (str, default='false')"
        return list_pods_wrapper
    
    elif tool_name == "get_pod_logs":
        def get_pod_logs_wrapper(pod_name: str, namespace: str = "default", lines: str = "50"):
            """Get logs from a specific pod for troubleshooting."""
            if not pod_name or not pod_name.strip():
                return "Error: Pod name is required as the first argument"
            return call_mcp_tool(tool_name, kwargs={'pod_name': pod_name, 'namespace': namespace, 'lines': lines})
        
        get_pod_logs_wrapper.__name__ = f"mcp_tool_{tool_name}"
        get_pod_logs_wrapper.__doc__ = f"Get logs from a pod. Args: pod_name (str, required), namespace (str, default='default'), lines (str, default='50')"
        return get_pod_logs_wrapper
    
    elif tool_name == "describe_pod":
        def describe_pod_wrapper(pod_name: str, namespace: str = "default"):
            """Get detailed information about a pod for troubleshooting."""
            if not pod_name or not pod_name.strip():
                return "Error: Pod name is required as the first argument"
            return call_mcp_tool(tool_name, kwargs={'pod_name': pod_name, 'namespace': namespace})
        
        describe_pod_wrapper.__name__ = f"mcp_tool_{tool_name}"
        describe_pod_wrapper.__doc__ = f"Describe a pod in detail. Args: pod_name (str, required), namespace (str, default='default')"
        return describe_pod_wrapper
    
    elif tool_name == "suggest_troubleshooting":
        def suggest_troubleshooting_wrapper(pod_name: str, namespace: str = "default"):
            """Analyze a pod and suggest troubleshooting steps based on its current state."""
            if not pod_name or not pod_name.strip():
                return "Error: Pod name is required as the first argument"
            return call_mcp_tool(tool_name, kwargs={'pod_name': pod_name, 'namespace': namespace})
        
        suggest_troubleshooting_wrapper.__name__ = f"mcp_tool_{tool_name}"
        suggest_troubleshooting_wrapper.__doc__ = f"Get troubleshooting suggestions for a pod. Args: pod_name (str, required), namespace (str, default='default')"
        return suggest_troubleshooting_wrapper
    
    elif tool_name == "network_connectivity_test":
        def network_connectivity_test_wrapper(target: str, port: str = "80"):
            """Test network connectivity to a target host and port from within the cluster."""
            if not target or not target.strip():
                return "Error: Target host/IP is required as the first argument"
            return call_mcp_tool(tool_name, kwargs={'target': target, 'port': port})
        
        network_connectivity_test_wrapper.__name__ = f"mcp_tool_{tool_name}"
        network_connectivity_test_wrapper.__doc__ = f"Test network connectivity to a target. Args: target (str, required), port (str, default='80')"
        return network_connectivity_test_wrapper
    
    elif tool_name == "get_cluster_info":
        def get_cluster_info_wrapper():
            """Get basic Kubernetes cluster information and health status."""
            return call_mcp_tool(tool_name)
        
        get_cluster_info_wrapper.__name__ = f"mcp_tool_{tool_name}"
        get_cluster_info_wrapper.__doc__ = f"Get cluster information and status"
        return get_cluster_info_wrapper
    
    elif tool_name == "get_service_status":
        def get_service_status_wrapper(namespace: str = "default"):
            """Get status of services in a namespace."""
            return call_mcp_tool(tool_name, kwargs={'namespace': namespace})
        
        get_service_status_wrapper.__name__ = f"mcp_tool_{tool_name}"
        get_service_status_wrapper.__doc__ = f"Get service status in a namespace. Args: namespace (str, default='default')"
        return get_service_status_wrapper
    
    elif tool_name == "get_gke_cluster_metrics":
        def get_gke_cluster_metrics_wrapper(project_id: str = "", cluster_name: str = "", zone: str = ""):
            """Get GKE cluster performance metrics and health status from within the cluster."""
            return call_mcp_tool(tool_name, kwargs={'project_id': project_id, 'cluster_name': cluster_name, 'zone': zone})
        
        get_gke_cluster_metrics_wrapper.__name__ = f"mcp_tool_{tool_name}"
        get_gke_cluster_metrics_wrapper.__doc__ = f"Get GKE cluster metrics. Args: project_id (str, optional), cluster_name (str, optional), zone (str, optional)"
        return get_gke_cluster_metrics_wrapper
    
    elif tool_name == "health_check":
        def health_check_wrapper():
            """Check if the MCP server is healthy and can access Kubernetes."""
            return call_mcp_tool(tool_name)
        
        health_check_wrapper.__name__ = f"mcp_tool_{tool_name}"
        health_check_wrapper.__doc__ = f"Check MCP server health"
        return health_check_wrapper
    
    elif tool_name == "get_deployments":
        def get_deployments_wrapper(namespace: str = "default", show_all: str = "false"):
            """Get deployments in a specific namespace with their status and replica information."""
            return call_mcp_tool(tool_name, kwargs={'namespace': namespace, 'show_all': show_all})
        
        get_deployments_wrapper.__name__ = f"mcp_tool_{tool_name}"
        get_deployments_wrapper.__doc__ = f"Get deployments in a namespace. Args: namespace (str, default='default'), show_all (str, default='false')"
        return get_deployments_wrapper
    
    elif tool_name == "scale_deployment":
        def scale_deployment_wrapper(deployment_name: str, replicas: str, namespace: str = "default"):
            """Scale a deployment to a specific number of replicas."""
            if not deployment_name or not deployment_name.strip():
                return "Error: Deployment name is required as the first argument"
            if not replicas or not replicas.strip():
                return "Error: Number of replicas is required as the second argument"
            return call_mcp_tool(tool_name, kwargs={'deployment_name': deployment_name, 'replicas': replicas, 'namespace': namespace})
        
        scale_deployment_wrapper.__name__ = f"mcp_tool_{tool_name}"
        scale_deployment_wrapper.__doc__ = f"Scale a deployment. Args: deployment_name (str, required), replicas (str, required), namespace (str, default='default')"
        return scale_deployment_wrapper
    
    elif tool_name == "create_deployment":
        def create_deployment_wrapper(deployment_name: str, image: str, namespace: str = "default", replicas: str = "1", port: str = ""):
            """Create a new deployment with specified image and configuration."""
            if not deployment_name or not deployment_name.strip():
                return "Error: Deployment name is required as the first argument"
            if not image or not image.strip():
                return "Error: Container image is required as the second argument"
            return call_mcp_tool(tool_name, kwargs={
                'deployment_name': deployment_name, 
                'image': image, 
                'namespace': namespace, 
                'replicas': replicas, 
                'port': port
            })
        
        create_deployment_wrapper.__name__ = f"mcp_tool_{tool_name}"
        create_deployment_wrapper.__doc__ = f"Create a deployment. Args: deployment_name (str, required), image (str, required), namespace (str, default='default'), replicas (str, default='1'), port (str, optional)"
        return create_deployment_wrapper
    
    elif tool_name == "delete_resource":
        def delete_resource_wrapper(resource_type: str, resource_name: str, namespace: str = "default"):
            """Delete a Kubernetes resource (deployment, service, pod, etc.)."""
            if not resource_type or not resource_type.strip():
                return "Error: Resource type is required as the first argument (e.g., 'deployment', 'service', 'pod')"
            if not resource_name or not resource_name.strip():
                return "Error: Resource name is required as the second argument"
            return call_mcp_tool(tool_name, kwargs={'resource_type': resource_type, 'resource_name': resource_name, 'namespace': namespace})
        
        delete_resource_wrapper.__name__ = f"mcp_tool_{tool_name}"
        delete_resource_wrapper.__doc__ = f"Delete a Kubernetes resource. Args: resource_type (str, required), resource_name (str, required), namespace (str, default='default')"
        return delete_resource_wrapper
    
    elif tool_name == "exec_pod_command":
        def exec_pod_command_wrapper(pod_name: str, command: str, namespace: str = "default", container: str = ""):
            """Execute a command inside a pod container."""
            if not pod_name or not pod_name.strip():
                return "Error: Pod name is required as the first argument"
            if not command or not command.strip():
                return "Error: Command is required as the second argument"
            return call_mcp_tool(tool_name, kwargs={'pod_name': pod_name, 'command': command, 'namespace': namespace, 'container': container})
        
        exec_pod_command_wrapper.__name__ = f"mcp_tool_{tool_name}"
        exec_pod_command_wrapper.__doc__ = f"Execute command in pod. Args: pod_name (str, required), command (str, required), namespace (str, default='default'), container (str, optional)"
        return exec_pod_command_wrapper
    
    elif tool_name == "apply_yaml":
        def apply_yaml_wrapper(yaml_content: str, namespace: str = "default"):
            """Apply Kubernetes YAML configuration."""
            if not yaml_content or not yaml_content.strip():
                return "Error: YAML content is required as the first argument"
            return call_mcp_tool(tool_name, kwargs={'yaml_content': yaml_content, 'namespace': namespace})
        
        apply_yaml_wrapper.__name__ = f"mcp_tool_{tool_name}"
        apply_yaml_wrapper.__doc__ = f"Apply YAML configuration. Args: yaml_content (str, required), namespace (str, default='default')"
        return apply_yaml_wrapper
    
    elif tool_name == "automate_remediation":
        def automate_remediation_wrapper(issue_type: str, resource_name: str = "", namespace: str = "default"):
            """Automatically remediate common Kubernetes issues."""
            if not issue_type or not issue_type.strip():
                return "Error: Issue type is required. Available types: 'restart_deployment', 'fix_image_pull', 'scale_zero_restart', 'clear_failed_pods'"
            return call_mcp_tool(tool_name, kwargs={'issue_type': issue_type, 'resource_name': resource_name, 'namespace': namespace})
        
        automate_remediation_wrapper.__name__ = f"mcp_tool_{tool_name}"
        automate_remediation_wrapper.__doc__ = f"Automate issue remediation. Args: issue_type (str, required), resource_name (str, optional), namespace (str, default='default')"
        return automate_remediation_wrapper
    
    else:
        # Generic wrapper for unknown tools
        def generic_wrapper(*args, **kwargs):
            """Generic wrapper for MCP tools"""
            return call_mcp_tool(tool_name, args=list(args), kwargs=kwargs)
        
        generic_wrapper.__name__ = f"mcp_tool_{tool_name}"
        generic_wrapper.__doc__ = f"Generic wrapper for MCP tool {tool_name}"
        return generic_wrapper


# Discover tools and create wrappers
logger.info("Discovering available MCP tools...")
available_tools = discover_tools()
tool_wrappers = []

# Define the tools we want to prioritize
priority_tools = [
    'get_cluster_info', 'list_pods', 'get_pod_logs', 'describe_pod', 
    'suggest_troubleshooting', 'network_connectivity_test', 
    'get_service_status', 'get_gke_cluster_metrics', 'get_deployments',
    'scale_deployment', 'create_deployment', 'delete_resource',
    'exec_pod_command', 'apply_yaml', 'automate_remediation'
]

# Create wrappers for priority tools first
for tool_name in priority_tools:
    if tool_name in available_tools:
        wrapper = make_tool_wrapper(tool_name)
        tool_wrappers.append(wrapper)
        logger.info(f"Created wrapper for priority tool: {tool_name}")

# Add any other discovered tools
for tool_name in available_tools:
    if tool_name not in priority_tools and tool_name != 'health_check':
        wrapper = make_tool_wrapper(tool_name)
        tool_wrappers.append(wrapper)
        logger.info(f"Created wrapper for additional tool: {tool_name}")

# Fallback if no tools were discovered
if not tool_wrappers:
    logger.warning("No tools discovered, creating fallback wrappers")
    fallback_tools = ['get_cluster_info', 'list_pods', 'get_pod_logs', 'describe_pod', 'get_deployments', 'scale_deployment']
    for tool_name in fallback_tools:
        wrapper = make_tool_wrapper(tool_name)
        tool_wrappers.append(wrapper)

logger.info(f"Created {len(tool_wrappers)} tool wrappers")

# Create agent instruction as a separate variable to avoid syntax issues
agent_instruction = """You are a Kubernetes troubleshooting expert agent with access to cluster management tools via MCP (Model Context Protocol).

Your capabilities include:

Information Gathering:
- Listing pods in namespaces and checking their status
- Getting detailed logs from specific pods  
- Describing pods to understand their configuration and state
- Getting deployments with replica status and health information
- Checking cluster information and metrics
- Checking service status
- Testing network connectivity from within the cluster

Troubleshooting and Analysis:
- Analyzing pods and providing troubleshooting suggestions
- Automated remediation for common issues

Management Actions:
- Scaling deployments up/down (changing replica count)
- Creating new deployments with specified images and configurations
- Deleting resources (deployments, services, pods, etc.)
- Executing commands inside pod containers
- Applying YAML configurations to the cluster
- Automated remediation actions for common problems

Automated Remediation Types:
- restart_deployment: Restart a deployment by triggering a rollout
- fix_image_pull: Analyze and suggest fixes for image pull issues
- scale_zero_restart: Scale deployment to 0 then back to original count (hard restart)
- clear_failed_pods: Remove all failed pods from a namespace

When users ask about Kubernetes issues:

For general cluster questions: Use get_cluster_info to understand the cluster state

For pod-related issues: 
   - First use list_pods to see available pods
   - Then use get_pod_logs or describe_pod with the specific pod name
   - Use suggest_troubleshooting for analysis and recommendations

For deployment management:
   - Use get_deployments to see deployment status and replica counts
   - Use scale_deployment to change replica counts for scaling up/down
   - Use create_deployment to create new deployments
   - Use delete_resource to remove deployments or other resources

For troubleshooting and remediation:
   - Use automate_remediation for common issues like restarting deployments
   - Use exec_pod_command to run diagnostic commands inside containers
   - Use apply_yaml to apply configuration changes

For network issues: Use network_connectivity_test to check connectivity

For service issues: Use get_service_status to check service configurations

Important Guidelines:
- Always provide required parameters when using tools that need them
- Don't say you can't fetch names - extract them from previous tool outputs or ask the user to specify
- When scaling deployments, always check current status first with get_deployments
- When deleting resources, confirm the resource type and name are correct
- For exec commands, use safe commands and avoid destructive operations
- When applying YAML, validate the configuration makes sense

Example Workflows:

1. Scaling a deployment: 
   - First: get_deployments to see current state
   - Then: scale_deployment with deployment name and desired replica count

2. Troubleshooting a failing pod:
   - First: list_pods to identify the problematic pod
   - Then: get_pod_logs and describe_pod for details
   - Finally: suggest_troubleshooting or automate_remediation

3. Creating and managing resources:
   - Use create_deployment for new deployments
   - Use apply_yaml for complex configurations
   - Use delete_resource for cleanup

Be helpful, thorough, and provide actionable steps based on tool results. Always explain what actions you're taking and why."""

# Create the ADK agent
agent = LlmAgent(
    model="gemini-2.0-flash",
    name="k8s_mcp_troubleshooting_agent",
    description="Kubernetes troubleshooting agent that can use MCP tools to diagnose and resolve cluster issues",
    instruction=agent_instruction,
    tools=tool_wrappers
)

# ADK will discover the root_agent instance
root_agent = agent