#!/usr/bin/env python3
"""
Kubernetes Management & GKE Monitoring MCP Server for GKE Deployment - Fixed Version
"""
import os
import sys
import logging
import json
import subprocess
import yaml
from datetime import datetime, timezone
from pathlib import Path
import httpx
import nmap
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import socket
import inspect
import asyncio
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from mcp.server.fastmcp import FastMCP

# Configure logging to stderr
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger("k8s-monitor-server-gke")

# Initialize MCP server for in-cluster deployment
mcp = FastMCP("k8s_monitor_gke")

# TOOL registry (populated later)
TOOL_FUNCTIONS = {}

def register_tool_functions():
    """Scan module globals for functions wrapped by @mcp.tool and register them."""
    known_tool_names = set([
        'health_check', 'get_cluster_info', 'list_pods', 'get_pod_logs', 'describe_pod',
        'get_service_status', 'get_deployment_status', 'suggest_troubleshooting',
        'network_connectivity_test', 'get_gke_cluster_metrics', 'get_deployments',
        'scale_deployment', 'create_deployment', 'delete_resource', 'exec_pod_command',
        'apply_yaml', 'automate_remediation'
    ])

    for name, obj in list(globals().items()):
        try:
            if callable(obj):
                # Prefer mcp.tool wrapped callables
                if getattr(obj, '__wrapped__', None) is not None:
                    TOOL_FUNCTIONS[name] = obj
                # Or functions whose name matches known tool names
                elif name in known_tool_names:
                    TOOL_FUNCTIONS[name] = obj
        except Exception:
            pass

    # Fallback health tool
    TOOL_FUNCTIONS.setdefault('health_check', lambda: 'ok')

    logger.info(f"Tool registry populated with: {sorted(list(TOOL_FUNCTIONS.keys()))}")

# Configuration - Use in-cluster config by default
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
GKE_CLUSTER_NAME = os.environ.get("GKE_CLUSTER_NAME", "")
GKE_ZONE = os.environ.get("GKE_ZONE", "")

# === UTILITY FUNCTIONS ===

def load_k8s_client():
    """Initialize Kubernetes client for in-cluster access"""
    try:
        # Try in-cluster config first (when running in pod)
        config.load_incluster_config()
        logger.info("Using in-cluster Kubernetes configuration")
    except:
        try:
            # Fallback to kubeconfig if available
            config.load_kube_config()
            logger.info("Using kubeconfig file")
        except Exception as e:
            logger.error(f"Failed to load k8s config: {e}")
            raise
    
    return client.CoreV1Api(), client.AppsV1Api(), client.NetworkingV1Api()

def safe_kubectl_command(cmd_args):
    """Execute kubectl command safely"""
    try:
        cmd = ["kubectl"] + cmd_args
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", "Command timed out"
    except Exception as e:
        return False, "", str(e)

def is_private_ip(ip):
    """Check if IP is in private range (for educational testing safety)"""
    import ipaddress
    try:
        ip_obj = ipaddress.ip_address(ip)
        return ip_obj.is_private
    except:
        return False


def _make_health_server(host: str, port: int):
    """Return an HTTPServer that responds 200 on / and /healthz"""
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/health", "/healthz"):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"ok")
            elif self.path == '/tools':
                # Return list of available tool names
                names = sorted(list(TOOL_FUNCTIONS.keys()))
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'tools': names}).encode('utf-8'))
            else:
                self.send_response(404)
                self.end_headers()
        def log_message(self, format, *args):
            return

        def do_POST(self):
            # POST /tool/<tool_name> with optional JSON body {"args": [...], "kwargs": {...}}
            if not self.path.startswith('/tool/'):
                self.send_response(404)
                self.end_headers()
                return

            tool_name = self.path[len('/tool/'):]
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length) if content_length else b''
            try:
                payload = json.loads(body.decode('utf-8')) if body else {}
            except Exception:
                payload = {}

            args = payload.get('args', []) or []
            kwargs = payload.get('kwargs', {}) or {}

            logger.info(f"HTTP tool call: {tool_name} with args={args}, kwargs={kwargs}")

            if tool_name not in TOOL_FUNCTIONS:
                self.send_response(404)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'ok': False, 'error': f'Tool {tool_name} not found'}).encode('utf-8'))
                return

            func = TOOL_FUNCTIONS[tool_name]
            try:
                # Call async functions via asyncio
                if asyncio.iscoroutinefunction(func):
                    result = asyncio.run(func(*args, **kwargs))
                else:
                    result = func(*args, **kwargs)
                # Ensure result is JSON-serializable
                if not isinstance(result, (str, int, float, dict, list, bool)):
                    result = str(result)

                # If the tool returned an error-like string, surface it as an HTTP 500
                def _looks_like_error(v):
                    if isinstance(v, str):
                        low = v.strip().lower()
                        if low.startswith('error') or 'unable to' in low or 'failed' in low or 'exception' in low:
                            return True
                    return False

                if _looks_like_error(result):
                    self.send_response(500)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'ok': False, 'error': str(result)}).encode('utf-8'))
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({'ok': True, 'result': result}).encode('utf-8'))
            except Exception as e:
                logger.error(f"Error calling tool {tool_name}: {e}", exc_info=True)
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'))

    server = HTTPServer((host, port), HealthHandler)
    server.allow_reuse_address = True
    return server


def start_probe_server_in_background(host: str = "0.0.0.0", port: int = 8080):
    """Start a lightweight HTTP health server in a daemon thread."""
    try:
        server = _make_health_server(host, port)
    except OSError as e:
        logger.warning(f"Health probe port {host}:{port} unavailable: {e}")
        return None

    def _serve():
        try:
            logger.info(f"Starting internal health server on {host}:{port}")
            server.serve_forever()
        except Exception as e:
            logger.error(f"Health server error: {e}", exc_info=True)
        finally:
            try:
                server.server_close()
            except:
                pass

    t = threading.Thread(target=_serve, name="health-probe-server", daemon=True)
    t.start()
    return server

# === HEALTH CHECK ===
@mcp.tool()
async def health_check() -> str:
    """Check if the MCP server is healthy and can access Kubernetes."""
    try:
        v1_core, _, _ = load_k8s_client()
        namespaces = v1_core.list_namespace(limit=1)
        pod_name = os.environ.get('HOSTNAME', 'unknown-pod')
        return f"MCP Server is healthy and connected to Kubernetes cluster from pod: {pod_name}"
    except Exception as e:
        return f"Health check failed: {str(e)}"

# === MCP TOOLS ===

@mcp.tool()
async def get_cluster_info() -> str:
    """Get basic Kubernetes cluster information and health status."""
    logger.info("Getting cluster information")
    
    try:
        v1_core, v1_apps, v1_net = load_k8s_client()
        
        # Get cluster version
        success, version_output, error = safe_kubectl_command(["version", "--short", "--client"])
        version_info = version_output if success else "Version unavailable"
        
        # Get nodes
        nodes = v1_core.list_node()
        node_info = []
        
        for node in nodes.items:
            status = "Ready" if any(condition.type == "Ready" and condition.status == "True" 
                                  for condition in node.status.conditions) else "Not Ready"
            
            # Get node resource capacity
            capacity = node.status.capacity or {}
            cpu_capacity = capacity.get('cpu', 'Unknown')
            memory_capacity = capacity.get('memory', 'Unknown')
            
            node_info.append(f"  - {node.metadata.name}: {status} (CPU: {cpu_capacity}, Memory: {memory_capacity})")
        
        # Get namespaces
        namespaces = v1_core.list_namespace()
        namespace_names = [ns.metadata.name for ns in namespaces.items]
        
        result = f"""Kubernetes Cluster Information (In-Cluster View):

Version Information:
{version_info}

Nodes ({len(node_info)}):
{chr(10).join(node_info) if node_info else '  No nodes found'}

Namespaces ({len(namespace_names)}):
  {', '.join(namespace_names[:10])}{'...' if len(namespace_names) > 10 else ''}

Cluster Status: Accessible from Pod
Running in: {os.environ.get('HOSTNAME', 'unknown-pod')}"""
        
        return result
        
    except Exception as e:
        logger.error(f"Error getting cluster info: {e}")
        return f"Error: Unable to connect to cluster: {str(e)}"

@mcp.tool()
async def list_pods(namespace: str = "default", show_all: str = "false") -> str:
    """List pods in a specific namespace with their status and resource usage."""
    logger.info(f"Listing pods in namespace: {namespace}")
    
    try:
        v1_core, v1_apps, v1_net = load_k8s_client()
        
        # List pods
        if show_all.lower() == "true":
            pods = v1_core.list_pod_for_all_namespaces()
            pods_list = pods.items
        else:
            pods = v1_core.list_namespaced_pod(namespace=namespace)
            pods_list = pods.items
        
        if not pods_list:
            return f"No pods found in namespace '{namespace}'"
        
        result = f"Pods in namespace '{namespace}' (viewed from in-cluster):\n\n"
        
        for pod in pods_list:
            pod_ns = pod.metadata.namespace if show_all.lower() == "true" else namespace
            status = pod.status.phase
            ready_containers = sum(1 for container in (pod.status.container_statuses or []) 
                                 if container.ready)
            total_containers = len(pod.spec.containers)
            
            # Get restart count
            restart_count = sum(container.restart_count for container in (pod.status.container_statuses or []))
            
            result += f"* {pod.metadata.name}"
            if show_all.lower() == "true":
                result += f" (ns: {pod_ns})"
            result += f"\n  Status: {status} | Ready: {ready_containers}/{total_containers}"
            if restart_count > 0:
                result += f" | Restarts: {restart_count}"
            result += f"\n  Age: {pod.metadata.creation_timestamp}\n\n"
        
        return result
        
    except ApiException as e:
        return f"Kubernetes API Error: {e.reason}"
    except Exception as e:
        logger.error(f"Error listing pods: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
async def get_pod_logs(pod_name: str, namespace: str = "default", lines: str = "50") -> str:
    """Get logs from a specific pod for troubleshooting.
    
    Args:
        pod_name: Name of the pod to get logs from (required)
        namespace: Kubernetes namespace (default: 'default')
        lines: Number of lines to retrieve (default: '50')
    """
    # Handle both positional and keyword arguments
    if not pod_name or not pod_name.strip():
        return "Error: Pod name is required. Please provide a pod name as the first argument."
    
    logger.info(f"Getting logs for pod: {pod_name} in namespace: {namespace}")
    
    try:
        tail_lines = int(lines) if lines and lines.strip() else 50
        
        success, logs, error = safe_kubectl_command([
            "logs", pod_name.strip(), 
            f"--namespace={namespace}", 
            f"--tail={tail_lines}"
        ])
        
        if success:
            return f"Logs for pod '{pod_name}' in namespace '{namespace}' (last {tail_lines} lines):\n\n{logs}"
        else:
            return f"Error getting logs for pod '{pod_name}': {error}"
        
    except ValueError:
        return f"Error: Invalid line count: {lines}"
    except Exception as e:
        logger.error(f"Error getting pod logs: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
async def describe_pod(pod_name: str, namespace: str = "default") -> str:
    """Get detailed information about a pod for troubleshooting.
    
    Args:
        pod_name: Name of the pod to describe (required)
        namespace: Kubernetes namespace (default: 'default')
    """
    # Handle both positional and keyword arguments
    if not pod_name or not pod_name.strip():
        return "Error: Pod name is required. Please provide a pod name as the first argument."
    
    logger.info(f"Describing pod: {pod_name} in namespace: {namespace}")
    
    try:
        success, output, error = safe_kubectl_command([
            "describe", "pod", pod_name.strip(), 
            f"--namespace={namespace}"
        ])
        
        if success:
            return f"Pod Details for '{pod_name}' in namespace '{namespace}' (from in-cluster view):\n\n{output}"
        else:
            return f"Error describing pod '{pod_name}': {error}"
        
    except Exception as e:
        logger.error(f"Error describing pod: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
async def get_service_status(namespace: str = "default") -> str:
    """Get status of services in a namespace."""
    logger.info(f"Getting service status for namespace: {namespace}")
    
    try:
        v1_core, v1_apps, v1_net = load_k8s_client()
        
        services = v1_core.list_namespaced_service(namespace=namespace)
        
        if not services.items:
            return f"No services found in namespace '{namespace}'"
        
        result = f"Services in namespace '{namespace}' (in-cluster view):\n\n"
        
        for svc in services.items:
            svc_type = svc.spec.type
            ports = []
            for port in svc.spec.ports or []:
                port_str = f"{port.port}"
                if port.target_port:
                    port_str += f"->{port.target_port}"
                if port.protocol and port.protocol != "TCP":
                    port_str += f"/{port.protocol}"
                ports.append(port_str)
            
            cluster_ip = svc.spec.cluster_ip or "None"
            external_ip = "None"
            
            if svc_type == "LoadBalancer" and svc.status.load_balancer.ingress:
                external_ip = svc.status.load_balancer.ingress[0].ip or svc.status.load_balancer.ingress[0].hostname
            elif svc_type == "NodePort":
                external_ip = "NodePort"
            
            result += f"* {svc.metadata.name}\n"
            result += f"  Type: {svc_type}\n"
            result += f"  Cluster IP: {cluster_ip}\n"
            result += f"  External IP: {external_ip}\n"
            result += f"  Ports: {', '.join(ports) if ports else 'None'}\n\n"
        
        return result
        
    except ApiException as e:
        return f"Kubernetes API Error: {e.reason}"
    except Exception as e:
        logger.error(f"Error getting service status: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
async def suggest_troubleshooting(pod_name: str, namespace: str = "default") -> str:
    """Analyze a pod and suggest troubleshooting steps based on its current state.
    
    Args:
        pod_name: Name of the pod to analyze (required)
        namespace: Kubernetes namespace (default: 'default')
    """
    # Handle both positional and keyword arguments
    if not pod_name or not pod_name.strip():
        return "Error: Pod name is required. Please provide a pod name as the first argument."
    
    logger.info(f"Analyzing pod for troubleshooting: {pod_name} in namespace: {namespace}")
    
    try:
        v1_core, v1_apps, v1_net = load_k8s_client()
        
        # Get pod details
        pod = v1_core.read_namespaced_pod(name=pod_name.strip(), namespace=namespace)
        
        suggestions = []
        status = pod.status.phase
        
        # Analyze pod status
        if status == "Pending":
            suggestions.append("Pod is Pending - Check these areas:")
            suggestions.append("  - Resource constraints: Check if cluster has enough CPU/memory")
            suggestions.append("  - Node selectors: Verify node selector requirements can be met")
            suggestions.append("  - PVC binding: Check if persistent volumes are available")
            suggestions.append("  - Image pull: Verify image exists and credentials are correct")
            
        elif status == "Failed":
            suggestions.append("Pod has Failed - Investigation steps:")
            suggestions.append(f"  - Check pod logs: kubectl logs {pod_name} -n {namespace}")
            suggestions.append("  - Review exit codes in container statuses")
            suggestions.append("  - Verify container image and command")
            suggestions.append("  - Check resource limits and requests")
            
        elif status == "Running":
            # Check container statuses
            container_issues = []
            if pod.status.container_statuses:
                for container in pod.status.container_statuses:
                    if not container.ready:
                        if container.restart_count > 0:
                            container_issues.append(f"  - {container.name}: High restart count ({container.restart_count})")
                        if container.state.waiting:
                            reason = container.state.waiting.reason
                            container_issues.append(f"  - {container.name}: Waiting - {reason}")
            
            if container_issues:
                suggestions.append("Container Issues Detected:")
                suggestions.extend(container_issues)
            else:
                suggestions.append("Pod appears healthy, but consider:")
                suggestions.append("  - Monitor resource usage patterns")
                suggestions.append("  - Check application logs for errors")
                suggestions.append("  - Verify network connectivity to dependencies")
        
        # In-cluster specific recommendations
        suggestions.append("\nIn-Cluster Remediation Steps:")
        suggestions.append("  1. Check resource quotas and limits")
        suggestions.append("  2. Verify network policies aren't blocking traffic")
        suggestions.append("  3. Review service mesh configuration if applicable")
        suggestions.append("  4. Check for node taints and tolerations")
        suggestions.append("  5. Monitor cluster-level issues (DNS, storage)")
        suggestions.append("  6. Validate service account permissions")
        
        return f"Troubleshooting suggestions for pod '{pod_name}' in namespace '{namespace}':\n\n" + "\n".join(suggestions)
        
    except ApiException as e:
        if e.status == 404:
            return f"Pod '{pod_name}' not found in namespace '{namespace}'"
        return f"Kubernetes API Error: {e.reason}"
    except Exception as e:
        logger.error(f"Error analyzing pod: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
async def network_connectivity_test(target: str, port: str = "80") -> str:
    """Test network connectivity to a target host and port from within the cluster.
    
    Args:
        target: Target host or IP address to test (required)
        port: Target port to test (default: '80')
    """
    # Handle both positional and keyword arguments
    if not target or not target.strip():
        return "Error: Target host/IP is required. Please provide a target as the first argument."
    
    logger.info(f"Testing connectivity to {target}:{port}")
    
    try:
        port_int = int(port) if port and port.strip() else 80
        target_clean = target.strip()
        
        # Use netcat for connection test
        result = subprocess.run(
            ["nc", "-z", "-v", "-w", "5", target_clean, str(port_int)],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        status = "Connection successful" if result.returncode == 0 else "Connection failed"
        
        # Additional DNS resolution test
        dns_result = subprocess.run(
            ["nslookup", target_clean],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        dns_info = "DNS resolution successful" if dns_result.returncode == 0 else "DNS resolution failed"
        
        response = f"Network Connectivity Test: {target_clean}:{port_int} (from in-cluster)\n\n"
        response += f"Port Connection: {status}\n"
        response += f"DNS Resolution: {dns_info}\n"
        response += f"Tested from pod: {os.environ.get('HOSTNAME', 'unknown-pod')}\n"
        
        if result.stderr:
            response += f"\nConnection Details:\n{result.stderr}"
        
        if dns_result.stdout:
            response += f"\nDNS Details:\n{dns_result.stdout}"
        
        return response
        
    except ValueError:
        return f"Error: Invalid port number: {port}"
    except subprocess.TimeoutExpired:
        return f"Connection test timed out for {target}:{port}"
    except Exception as e:
        logger.error(f"Error testing connectivity: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
async def get_gke_cluster_metrics(project_id: str = "", cluster_name: str = "", zone: str = "") -> str:
    """Get GKE cluster performance metrics and health status from within the cluster."""
    project = project_id.strip() or GCP_PROJECT_ID
    cluster = cluster_name.strip() or GKE_CLUSTER_NAME
    cluster_zone = zone.strip() or GKE_ZONE
    
    logger.info(f"Getting GKE metrics for cluster: {cluster}")
    
    try:
        # Use kubectl commands to get metrics from within the cluster
        success, output, error = safe_kubectl_command(["top", "nodes"])
        node_metrics = output if success else "Node metrics unavailable"
        
        success2, pod_output, error2 = safe_kubectl_command(["top", "pods", "--all-namespaces"])
        pod_metrics = pod_output if success2 else "Pod metrics unavailable"
        
        # Get cluster basic info
        success3, cluster_info, error3 = safe_kubectl_command(["cluster-info"])
        cluster_details = cluster_info if success3 else "Cluster info unavailable"
        
        # Get current pod's resource usage
        current_pod = os.environ.get('HOSTNAME', 'unknown-pod')
        success4, current_pod_metrics, error4 = safe_kubectl_command([
            "top", "pod", current_pod, "--namespace=default"
        ])
        self_metrics = current_pod_metrics if success4 else "Self metrics unavailable"
        
        result = f"GKE Cluster Metrics: {cluster} (In-Cluster View)\n\n"
        result += f"Node Resource Usage:\n{node_metrics}\n\n"
        result += f"Pod Resource Usage:\n{pod_metrics}\n\n"
        result += f"Cluster Information:\n{cluster_details}\n\n"
        result += f"Current Pod Metrics ({current_pod}):\n{self_metrics}\n\n"
        
        # Add suggestions based on metrics
        result += "Performance Insights (In-Cluster):\n"
        if "unavailable" not in node_metrics.lower():
            result += "  - Node metrics are available\n"
            result += "  - Monitor CPU/Memory usage trends\n"
            result += "  - Consider horizontal pod autoscaling for high usage\n"
            result += "  - This MCP server has cluster-wide visibility\n"
        else:
            result += "  - Metrics server may not be installed\n"
            result += "  - Install metrics-server: kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml\n"
        
        return result
        
    except Exception as e:
        logger.error(f"Error getting GKE metrics: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
async def get_deployments(namespace: str = "default", show_all: str = "false") -> str:
    """Get deployments in a specific namespace with their status and replica information.
    
    Args:
        namespace: Kubernetes namespace (default: 'default')
        show_all: Show deployments from all namespaces (default: 'false')
    """
    logger.info(f"Getting deployments in namespace: {namespace}")
    
    try:
        v1_core, v1_apps, v1_net = load_k8s_client()
        
        # List deployments
        if show_all.lower() == "true":
            deployments = v1_apps.list_deployment_for_all_namespaces()
            deployments_list = deployments.items
        else:
            deployments = v1_apps.list_namespaced_deployment(namespace=namespace)
            deployments_list = deployments.items
        
        if not deployments_list:
            return f"No deployments found in namespace '{namespace}'"
        
        result = f"Deployments in namespace '{namespace}' (viewed from in-cluster):\n\n"
        
        for deployment in deployments_list:
            deploy_ns = deployment.metadata.namespace if show_all.lower() == "true" else namespace
            
            # Get deployment status
            replicas = deployment.spec.replicas or 0
            ready_replicas = deployment.status.ready_replicas or 0
            available_replicas = deployment.status.available_replicas or 0
            unavailable_replicas = deployment.status.unavailable_replicas or 0
            
            # Check deployment condition
            conditions = deployment.status.conditions or []
            condition_status = "Unknown"
            for condition in conditions:
                if condition.type == "Available":
                    condition_status = "Available" if condition.status == "True" else "Unavailable"
                    break
            
            result += f"* {deployment.metadata.name}"
            if show_all.lower() == "true":
                result += f" (ns: {deploy_ns})"
            result += f"\n  Replicas: {ready_replicas}/{replicas} ready"
            if available_replicas != ready_replicas:
                result += f" | Available: {available_replicas}"
            if unavailable_replicas > 0:
                result += f" | Unavailable: {unavailable_replicas}"
            result += f"\n  Status: {condition_status}"
            result += f"\n  Age: {deployment.metadata.creation_timestamp}"
            result += f"\n  Image: {deployment.spec.template.spec.containers[0].image if deployment.spec.template.spec.containers else 'N/A'}\n\n"
        
        return result
        
    except ApiException as e:
        return f"Kubernetes API Error: {e.reason}"
    except Exception as e:
        logger.error(f"Error getting deployments: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
async def scale_deployment(deployment_name: str, replicas: str, namespace: str = "default") -> str:
    """Scale a deployment to a specific number of replicas.
    
    Args:
        deployment_name: Name of the deployment to scale (required)
        replicas: Number of replicas to scale to (required)
        namespace: Kubernetes namespace (default: 'default')
    """
    if not deployment_name or not deployment_name.strip():
        return "Error: Deployment name is required as the first argument"
    
    if not replicas or not replicas.strip():
        return "Error: Number of replicas is required as the second argument"
    
    try:
        replica_count = int(replicas.strip())
        if replica_count < 0:
            return "Error: Replica count cannot be negative"
    except ValueError:
        return f"Error: Invalid replica count: {replicas}"
    
    logger.info(f"Scaling deployment {deployment_name} to {replica_count} replicas in namespace {namespace}")
    
    try:
        success, output, error = safe_kubectl_command([
            "scale", "deployment", deployment_name.strip(),
            f"--replicas={replica_count}",
            f"--namespace={namespace}"
        ])
        
        if success:
            return f"Successfully scaled deployment '{deployment_name}' to {replica_count} replicas in namespace '{namespace}':\n{output}"
        else:
            return f"Error scaling deployment '{deployment_name}': {error}"
        
    except Exception as e:
        logger.error(f"Error scaling deployment: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
async def create_deployment(deployment_name: str, image: str, namespace: str = "default", replicas: str = "1", port: str = "") -> str:
    """Create a new deployment with specified image and configuration.
    
    Args:
        deployment_name: Name of the deployment to create (required)
        image: Container image to use (required)
        namespace: Kubernetes namespace (default: 'default')
        replicas: Number of replicas (default: '1')
        port: Container port to expose (optional)
    """
    if not deployment_name or not deployment_name.strip():
        return "Error: Deployment name is required as the first argument"
    
    if not image or not image.strip():
        return "Error: Container image is required as the second argument"
    
    logger.info(f"Creating deployment {deployment_name} with image {image} in namespace {namespace}")
    
    try:
        replica_count = int(replicas.strip()) if replicas.strip() else 1
        if replica_count < 0:
            return "Error: Replica count cannot be negative"
    except ValueError:
        return f"Error: Invalid replica count: {replicas}"
    
    try:
        cmd = [
            "create", "deployment", deployment_name.strip(),
            f"--image={image.strip()}",
            f"--replicas={replica_count}",
            f"--namespace={namespace}"
        ]
        
        if port and port.strip():
            try:
                port_int = int(port.strip())
                cmd.extend([f"--port={port_int}"])
            except ValueError:
                return f"Error: Invalid port number: {port}"
        
        success, output, error = safe_kubectl_command(cmd)
        
        if success:
            return f"Successfully created deployment '{deployment_name}' in namespace '{namespace}':\n{output}"
        else:
            return f"Error creating deployment '{deployment_name}': {error}"
        
    except Exception as e:
        logger.error(f"Error creating deployment: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
async def delete_resource(resource_type: str, resource_name: str, namespace: str = "default") -> str:
    """Delete a Kubernetes resource (deployment, service, pod, etc.).
    
    Args:
        resource_type: Type of resource to delete (e.g., 'deployment', 'service', 'pod') (required)
        resource_name: Name of the resource to delete (required)
        namespace: Kubernetes namespace (default: 'default')
    """
    if not resource_type or not resource_type.strip():
        return "Error: Resource type is required as the first argument (e.g., 'deployment', 'service', 'pod')"
    
    if not resource_name or not resource_name.strip():
        return "Error: Resource name is required as the second argument"
    
    # Validate resource type for safety
    allowed_types = ['deployment', 'service', 'pod', 'configmap', 'secret', 'ingress', 'pvc', 'job', 'cronjob']
    if resource_type.strip().lower() not in allowed_types:
        return f"Error: Resource type '{resource_type}' not allowed. Allowed types: {', '.join(allowed_types)}"
    
    logger.info(f"Deleting {resource_type} {resource_name} in namespace {namespace}")
    
    try:
        success, output, error = safe_kubectl_command([
            "delete", resource_type.strip().lower(), resource_name.strip(),
            f"--namespace={namespace}"
        ])
        
        if success:
            return f"Successfully deleted {resource_type} '{resource_name}' from namespace '{namespace}':\n{output}"
        else:
            return f"Error deleting {resource_type} '{resource_name}': {error}"
        
    except Exception as e:
        logger.error(f"Error deleting resource: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
async def exec_pod_command(pod_name: str, command: str, namespace: str = "default", container: str = "") -> str:
    """Execute a command inside a pod container.
    
    Args:
        pod_name: Name of the pod to exec into (required)
        command: Command to execute (required)
        namespace: Kubernetes namespace (default: 'default')
        container: Specific container name (optional, uses first container if not specified)
    """
    if not pod_name or not pod_name.strip():
        return "Error: Pod name is required as the first argument"
    
    if not command or not command.strip():
        return "Error: Command is required as the second argument"
    
    # Basic safety check - don't allow potentially dangerous commands
    dangerous_commands = ['rm -rf', 'dd if=', 'mkfs', 'fdisk', 'format', 'shutdown', 'reboot', 'halt']
    cmd_lower = command.strip().lower()
    for dangerous in dangerous_commands:
        if dangerous in cmd_lower:
            return f"Error: Command '{command}' is not allowed for security reasons"
    
    logger.info(f"Executing command in pod {pod_name}: {command}")
    
    try:
        cmd = ["exec", pod_name.strip(), f"--namespace={namespace}"]
        
        if container and container.strip():
            cmd.extend(["-c", container.strip()])
        
        cmd.append("--")
        # Split command by spaces for safety
        cmd.extend(command.strip().split())
        
        success, output, error = safe_kubectl_command(cmd)
        
        if success:
            return f"Command executed successfully in pod '{pod_name}':\n\nOutput:\n{output}"
        else:
            return f"Error executing command in pod '{pod_name}': {error}"
        
    except Exception as e:
        logger.error(f"Error executing command: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
async def apply_yaml(yaml_content: str, namespace: str = "default") -> str:
    """Apply Kubernetes YAML configuration.
    
    Args:
        yaml_content: YAML content to apply (required)
        namespace: Target namespace (default: 'default')
    """
    if not yaml_content or not yaml_content.strip():
        return "Error: YAML content is required as the first argument"
    
    logger.info(f"Applying YAML configuration to namespace {namespace}")
    
    try:
        # Write YAML content to a temporary file
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as temp_file:
            temp_file.write(yaml_content.strip())
            temp_file_path = temp_file.name
        
        try:
            cmd = ["apply", "-f", temp_file_path, f"--namespace={namespace}"]
            success, output, error = safe_kubectl_command(cmd)
            
            if success:
                return f"Successfully applied YAML configuration to namespace '{namespace}':\n{output}"
            else:
                return f"Error applying YAML configuration: {error}"
        finally:
            # Clean up temp file
            try:
                os.unlink(temp_file_path)
            except:
                pass
        
    except Exception as e:
        logger.error(f"Error applying YAML: {e}")
        return f"Error: {str(e)}"

@mcp.tool()
async def automate_remediation(issue_type: str, resource_name: str = "", namespace: str = "default", **kwargs) -> str:
    """Automatically remediate common Kubernetes issues.
    
    Args:
        issue_type: Type of issue to remediate (required)
        resource_name: Name of the affected resource (optional, depends on issue type)
        namespace: Kubernetes namespace (default: 'default')
        **kwargs: Additional parameters based on issue type
    """
    if not issue_type or not issue_type.strip():
        return "Error: Issue type is required. Available types: 'restart_deployment', 'fix_image_pull', 'scale_zero_restart', 'clear_failed_pods'"
    
    issue = issue_type.strip().lower()
    logger.info(f"Attempting automated remediation for issue: {issue}")
    
    try:
        if issue == "restart_deployment":
            if not resource_name:
                return "Error: Deployment name is required for restart_deployment"
            
            # Restart deployment by adding/updating an annotation
            success, output, error = safe_kubectl_command([
                "patch", "deployment", resource_name.strip(),
                "-p", f'{{"spec":{{"template":{{"metadata":{{"annotations":{{"kubectl.kubernetes.io/restartedAt":"{datetime.now(timezone.utc).isoformat()}"}}}}}}}}}}',
                f"--namespace={namespace}"
            ])
            
            if success:
                return f"Successfully triggered restart for deployment '{resource_name}' in namespace '{namespace}':\n{output}"
            else:
                return f"Error restarting deployment '{resource_name}': {error}"
        
        elif issue == "fix_image_pull":
            if not resource_name:
                return "Error: Pod name is required for fix_image_pull"
            
            # Get pod details and suggest image pull fixes
            success, output, error = safe_kubectl_command([
                "describe", "pod", resource_name.strip(),
                f"--namespace={namespace}"
            ])
            
            remediation_steps = []
            if success:
                if "ImagePullBackOff" in output or "ErrImagePull" in output:
                    remediation_steps.append("Image pull issue detected. Possible fixes:")
                    remediation_steps.append("1. Check if image name and tag are correct")
                    remediation_steps.append("2. Verify image registry credentials")
                    remediation_steps.append("3. Check network connectivity to registry")
                    remediation_steps.append("4. Validate image exists in registry")
                    
                    # Try to get more info about the deployment
                    deploy_success, deploy_output, deploy_error = safe_kubectl_command([
                        "get", "pod", resource_name.strip(),
                        "-o", "jsonpath={.spec.containers[0].image}",
                        f"--namespace={namespace}"
                    ])
                    
                    if deploy_success:
                        remediation_steps.append(f"\nCurrent image: {deploy_output}")
                
                return f"Image pull remediation analysis for pod '{resource_name}':\n\n" + "\n".join(remediation_steps)
            else:
                return f"Error analyzing pod '{resource_name}': {error}"
        
        elif issue == "scale_zero_restart":
            if not resource_name:
                return "Error: Deployment name is required for scale_zero_restart"
            
            # Scale to 0 then back to original replica count
            # First get current replica count
            success, current_replicas, error = safe_kubectl_command([
                "get", "deployment", resource_name.strip(),
                "-o", "jsonpath={.spec.replicas}",
                f"--namespace={namespace}"
            ])
            
            if not success:
                return f"Error getting current replica count for '{resource_name}': {error}"
            
            try:
                original_replicas = int(current_replicas.strip()) if current_replicas.strip() else 1
            except ValueError:
                original_replicas = 1
            
            # Scale to 0
            success1, output1, error1 = safe_kubectl_command([
                "scale", "deployment", resource_name.strip(),
                "--replicas=0",
                f"--namespace={namespace}"
            ])
            
            if not success1:
                return f"Error scaling deployment '{resource_name}' to 0: {error1}"
            
            # Wait a moment
            time.sleep(2)
            
            # Scale back to original
            success2, output2, error2 = safe_kubectl_command([
                "scale", "deployment", resource_name.strip(),
                f"--replicas={original_replicas}",
                f"--namespace={namespace}"
            ])
            
            if success2:
                return f"Successfully performed zero-scale restart for deployment '{resource_name}':\nScaled to 0: {output1}\nScaled to {original_replicas}: {output2}"
            else:
                return f"Error scaling deployment '{resource_name}' back to {original_replicas}: {error2}"
        
        elif issue == "clear_failed_pods":
            # Delete all failed pods in the namespace
            success, failed_pods, error = safe_kubectl_command([
                "get", "pods",
                f"--namespace={namespace}",
                "--field-selector=status.phase=Failed",
                "-o", "jsonpath={.items[*].metadata.name}"
            ])
            
            if not success:
                return f"Error getting failed pods: {error}"
            
            if not failed_pods.strip():
                return f"No failed pods found in namespace '{namespace}'"
            
            pod_names = failed_pods.strip().split()
            results = []
            
            for pod_name in pod_names:
                success, output, error = safe_kubectl_command([
                    "delete", "pod", pod_name,
                    f"--namespace={namespace}"
                ])
                
                if success:
                    results.append(f"✓ Deleted failed pod: {pod_name}")
                else:
                    results.append(f"✗ Failed to delete pod {pod_name}: {error}")
            
            return f"Failed pod cleanup results:\n" + "\n".join(results)
        
        else:
            return f"Unknown issue type: {issue}. Available types: 'restart_deployment', 'fix_image_pull', 'scale_zero_restart', 'clear_failed_pods'"
    
    except Exception as e:
        logger.error(f"Error in automated remediation: {e}")
        return f"Error during remediation: {str(e)}"

# === SERVER STARTUP ===

def main():
    """Main server startup function."""
    logger.info("Starting Kubernetes MCP server for GKE deployment...")
    
    # Verify in-cluster access
    try:
        load_k8s_client()
        logger.info("Successfully connected to Kubernetes cluster")
    except Exception as e:
        logger.error(f"Failed to connect to cluster: {e}")
        sys.exit(1)
    
    # Log environment info
    logger.info(f"Pod hostname: {os.environ.get('HOSTNAME', 'unknown')}")
    logger.info(f"Namespace: {os.environ.get('POD_NAMESPACE', 'default')}")
    
    # Decide transport: force SSE when running in-cluster so the pod serves HTTP for probes
    in_cluster = bool(os.environ.get('KUBERNETES_SERVICE_HOST') or os.path.exists('/var/run/secrets/kubernetes.io/serviceaccount'))
    if in_cluster:
        transport = os.environ.get('MCP_TRANSPORT', 'sse')
    else:
        transport = os.environ.get('MCP_TRANSPORT', 'stdio')

    host = os.environ.get('MCP_HOST', '0.0.0.0')
    port = int(os.environ.get('MCP_PORT', '8080'))

    logger.info(f"Pod hostname: {os.environ.get('HOSTNAME', 'unknown')}")
    logger.info(f"Namespace: {os.environ.get('POD_NAMESPACE', 'default')}")
    logger.info(f"Configured transport={transport}, MCP_HOST={host}, MCP_PORT={port}")

    # Ensure env vars exist for libraries that read them
    os.environ.setdefault('MCP_HOST', host)
    os.environ.setdefault('MCP_PORT', str(port))

    # Register tool functions so the probe server can expose them
    try:
        register_tool_functions()
        logger.info(f"Registered tools for HTTP proxy: {sorted(list(TOOL_FUNCTIONS.keys()))}")
    except Exception as e:
        logger.warning(f"Failed to register tool functions: {e}")

    # Start internal probe server so readiness/liveness probes can hit :8080
    start_probe_server_in_background(host=host, port=port)

    try:
        if transport == 'stdio':
            logger.info("Starting MCP server with stdio transport")
            mcp.run(transport='stdio')
        else:
            logger.info("Attempting to start MCP server with SSE transport")

            # Detect whether mcp.run accepts host/port kwargs and call accordingly
            try:
                sig = inspect.signature(mcp.run)
                params = sig.parameters
                if 'host' in params and 'port' in params:
                    logger.info(f"Calling mcp.run with host={host} port={port}")
                    mcp.run(transport='sse', host=host, port=port)
                else:
                    logger.info("mcp.run does not accept host/port kwargs; calling without kwargs")
                    mcp.run(transport='sse')
            except Exception:
                # If inspection fails, call without kwargs (safe fallback)
                logger.info("Could not inspect mcp.run signature; calling without kwargs")
                mcp.run(transport='sse')

        # If mcp.run() returns (it should normally block), keep process alive so pod doesn't exit
        logger.info("mcp.run() returned; entering keep-alive loop to prevent container exit")
        while True:
            time.sleep(60)

    except TypeError as te:
        logger.error(f"TypeError running MCP server: {te}", exc_info=True)
        logger.info("Retrying mcp.run without host/port kwargs as fallback")
        try:
            mcp.run(transport='sse')
            while True:
                time.sleep(60)
        except Exception as e:
            logger.error(f"Failed to start MCP server on retry: {e}", exc_info=True)
            sys.exit(1)
    except Exception as e:
        logger.error(f"Server error while starting MCP: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()