"""Role packs registry."""

from typing import Dict, Type
from orchestrator.packs.business_idea import BusinessIdeaPack, PackTaskSpec
from orchestrator.packs.developer import DeveloperPack
from orchestrator.packs.qa import QAPack

PACKS: Dict[str, Type] = {
    "business_idea": BusinessIdeaPack,
    "developer": DeveloperPack,
    "qa": QAPack
}
