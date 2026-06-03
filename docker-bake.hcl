group "default" {
  targets = ["temms-agent"]
}

target "temms-agent" {
  context = "."
  dockerfile = "Dockerfile"
  tags = ["temms/agent:local"]
  platforms = [
    "linux/amd64",
    "linux/arm64",
  ]
}
