variable "subscription_id" {
  description = "Azure subscription ID (kde žije Foundry)"
  type        = string
  default     = "d96e1291-0b9a-4f15-a392-a63136aabcae"
}

variable "resource_group_name" {
  description = "Existující RG, kterou pouze čteme — nespravujeme"
  type        = string
  default     = "ptuc-foundry"
}

variable "location" {
  description = "Azure region (kde běží Foundry kvůli low-latency)"
  type        = string
  default     = "swedencentral"
}

variable "app_name" {
  description = "Jméno Container Appu (a prefix pro ostatní resources)"
  type        = string
  default     = "noty-app"
}

variable "image_tag" {
  description = "Tag image v ACR (zatím \"latest\", v CI nahradí git SHA)"
  type        = string
  default     = "latest"
}

variable "foundry_resource_name" {
  description = "Jméno MS Foundry resource (pro Claude API)"
  type        = string
  default     = "ptuc-foundry-test"
}

variable "foundry_api_key" {
  description = "Foundry API key — předáváno přes -var nebo env TF_VAR_foundry_api_key (NEPATŘÍ do gitu)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "llm_cleanup_enabled" {
  description = "Zapnout LLM cleanup (vyžaduje API key nebo Managed Identity)"
  type        = bool
  default     = true
}

variable "cleanup_model" {
  description = "Claude model name (Opus 4.7 / 4.6 / Sonnet 4.6 / Haiku 4.5)"
  type        = string
  default     = "claude-opus-4-7"
}

variable "custom_hostname" {
  description = "Custom doména pro Container App (volitelná)"
  type        = string
  default     = "noty.davidtucek.cz"
}

variable "use_managed_identity_for_foundry" {
  description = "Pokud true: System-Assigned MI + role assignment místo API key"
  type        = bool
  default     = false
}
