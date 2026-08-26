"""Pruebas que necesitan datos que este repo no publica.

Este repositorio trae la HERRAMIENTA. La evaluación real -- los `config.yaml`
de las plataformas medidas, sus mapas de superficies y la evidencia capturada --
no se publica, así que las pruebas que leen esos archivos no tienen contra qué
correr. Saltan con motivo, no fallan: una suite en rojo al clonar diría que la
herramienta está rota, y no lo está. Poné tu propia plataforma en
`platforms/<nombre>/config.yaml` (hay un ejemplo en `platforms/ejemplo/`) y las
que dependen de configuración empiezan a correr solas.
"""
import pytest

SIN_DATOS = {
    "test_navigation_comes_from_the_platform_not_the_probe",
    "test_a_sequence_becomes_one_send_and_one_read_per_stimulus",
    "test_an_unavailable_surface_refuses_instead_of_substituting",
    "test_a_read_only_probe_still_reads_the_surface",
    "test_a_privacy_probe_reads_the_published_policy_not_the_app",
    "test_detect_course_passes_on_the_captured_plataforma_a_fixture",
    "test_shipped_evidence_map_loads_and_every_artifact_exists",
    "test_dotenv_is_read_from_the_same_file_config_uses",
    "test_the_shipped_platform_configs_carry_their_auth_block",
    "test_plataforma_a_can_tell_it_is_logged_out_on_a_provider_chooser",
    "test_the_chooser_steps_are_read_from_the_plataforma_a_config",
    "test_the_role_switch_is_read_from_the_plataforma_a_config",
    "test_the_vendor_and_the_people_are_gone",
    # Necesitan el surfaces.yaml de una plataforma medida.
    "test_a_banked_probe_is_used_verbatim_not_redrafted",
    "test_a_criterion_without_a_banked_probe_still_drafts_normally",
    # Importa desde scripts/, que es el andamio de la evaluación y no viaja.
    "test_operative_bank_text_is_in_spanish",
}


def pytest_collection_modifyitems(items):
    for item in items:
        if item.name.split("[")[0] in SIN_DATOS:
            item.add_marker(
                pytest.mark.skip(
                    reason="necesita datos de una evaluación real, que este "
                    "repo no publica"
                )
            )
