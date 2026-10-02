"""Performance baseline measurement script."""

import statistics
import time
import urllib.request


def measure(url: str, n: int = 50):
    latencies = []
    for _ in range(n):
        t0 = time.perf_counter()
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as resp:
            resp.read()
        latencies.append((time.perf_counter() - t0) * 1000)
    return latencies


if __name__ == "__main__":
    # Warmup
    for _ in range(5):
        urllib.request.urlopen("http://localhost:8000/health")
        urllib.request.urlopen("http://localhost:8000/ready")

    health_times = measure("http://localhost:8000/health", 50)
    ready_times = measure("http://localhost:8000/ready", 50)

    print("\n=== Latency Benchmark (50 requests each) ===")
    print("GET /health (Liveness):")
    print(f"  Min:    {min(health_times):.2f} ms")
    print(f"  Avg:    {statistics.mean(health_times):.2f} ms")
    print(f"  Median: {statistics.median(health_times):.2f} ms")
    print(f"  Max:    {max(health_times):.2f} ms")
    print(f"  StdDev: {statistics.stdev(health_times):.2f} ms")

    print("\nGET /ready (Readiness: API -> PG + Redis):")
    print(f"  Min:    {min(ready_times):.2f} ms")
    print(f"  Avg:    {statistics.mean(ready_times):.2f} ms")
    print(f"  Median: {statistics.median(ready_times):.2f} ms")
    print(f"  Max:    {max(ready_times):.2f} ms")
    print(f"  StdDev: {statistics.stdev(ready_times):.2f} ms")

    # Throughput estimation (requests per second on sequential loop)
    total_time = sum(health_times) / 1000.0
    rps = 50 / total_time
    print(f"\nSequential Throughput (GET /health): {rps:.1f} req/sec")
