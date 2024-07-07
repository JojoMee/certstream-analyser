import matplotlib.pyplot as plt
import numpy as np

# Constants
simulate_days = 365
total_disk_space_gb = 1798.43  # Total available disk space in GB
total_disk_space_bytes = total_disk_space_gb * 1024**3  # Convert GB to Bytes
seconds_per_day = 86400  # Number of seconds in a day

print(f"Total disk space {total_disk_space_bytes:_.0f} byte ({total_disk_space_gb}GB)")

# Scenarios
scenarios = [
    {
        "growth_rate_per_second": 290,
        "bytes_per_document": 894,
        "label": "Scenario 1: 290c/s @ 894b/c",
    },
    {
        "growth_rate_per_second": 290,
        "bytes_per_document": 947,
        "label": "Scenario 2: 290c/s @ 947b/c",
    },
    {
        "growth_rate_per_second": 331,
        "bytes_per_document": 894,
        "label": "Scenario 3: 331c/s @ 894b/c",
    },
    {
        "growth_rate_per_second": 331,
        "bytes_per_document": 947,
        "label": "Scenario 4: 331c/s @ 947b/c",
    },
]

# Simulate document growth over time (e.g., per day)
days = np.arange(1, simulate_days + 1)

plt.figure(figsize=(10, 6))

for scenario in scenarios:
    growth_rate_per_second = scenario["growth_rate_per_second"]
    bytes_per_document = scenario["bytes_per_document"]

    # Calculate the number of documents added per day
    documents_per_day = growth_rate_per_second * seconds_per_day

    # Calculate used disk space over time
    documents = documents_per_day * days
    used_space_gb = (documents * bytes_per_document) / 1024**3

    # Plot each scenario
    plt.plot(days, used_space_gb, label=scenario["label"])

    # Determine the break-even point
    print(f"{scenario['label']}:")
    break_even_day = np.argmax(used_space_gb >= total_disk_space_gb)
    if used_space_gb[break_even_day] >= total_disk_space_gb:
        print(f"  Break-even point: Day {break_even_day}")
    else:
        print(f"  Break-even point: Not within {simulate_days} days")

    print(f"  Storage needed for a year: {used_space_gb[364]:.0f}GB")
    print(f"  Total certificates per year: {documents[364]:_}")

# Plot total disk space line
plt.axhline(
    y=total_disk_space_gb,
    color="r",
    # linestyle="--",
    label=f"Available Disk Space ({total_disk_space_gb:.0f}GB)",
)

plt.xlabel("Days")
plt.ylabel("Disk Space Used (GB)")
plt.title("Disk Space Usage Over Time for Different Scenarios")
plt.legend()
plt.grid(True)
plt.savefig("disk_space.pdf")
plt.show()
