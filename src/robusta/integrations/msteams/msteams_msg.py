import json
import logging
from typing import List

import requests

from robusta.core.reporting import (
    FileBlock,
    Finding,
    FindingSeverity,
    HeaderBlock,
    KubernetesDiffBlock,
    ListBlock,
    MarkdownBlock,
    TableBlock,
)
from robusta.core.reporting.base import FindingStatus, LinkType
from robusta.core.reporting.url_helpers import convert_prom_graph_url_to_robusta_metrics_explorer
from robusta.integrations.msteams.msteams_adaptive_card_files import MsTeamsAdaptiveCardFiles
from robusta.integrations.msteams.msteams_elements.msteams_base import MsTeamsBase
from robusta.integrations.msteams.msteams_elements.msteams_card import MsTeamsCard
from robusta.integrations.msteams.msteams_elements.msteams_column import MsTeamsColumn
from robusta.integrations.msteams.msteams_elements.msteams_images import MsTeamsImages
from robusta.integrations.msteams.msteams_elements.msteams_table import MsTeamsTable
from robusta.integrations.msteams.msteams_elements.msteams_text_block import MsTeamsTextBlock


class MsTeamsMsg:
    # actual size according to the DOC is ~28K.
    # it's hard to determine the real size because for example there can be large images that doesn't count
    # and converting the map to json doesn't give us an exact indication of the size so we need to take
    # a safe zone of less then 28K
    MAX_SIZE_IN_BYTES = 1024 * 20

    def __init__(self, webhook_url: str, prefer_redirect_to_platform: bool):
        self.entire_msg: List[MsTeamsBase] = []
        self.current_section: List[MsTeamsBase] = []
        self.text_file_containers = []
        self.webhook_url = webhook_url
        self.prefer_redirect_to_platform = prefer_redirect_to_platform

    def write_title_and_desc(self, platform_enabled: bool, finding: Finding, cluster_name: str, account_id: str):
        status: FindingStatus = (
            FindingStatus.RESOLVED if finding.title.startswith("[RESOLVED]") else FindingStatus.FIRING
        )
        title = finding.title.removeprefix("[RESOLVED] ")
        title = self.__build_msteams_title(title, status, finding.severity, finding.add_silence_url)

        block = MsTeamsTextBlock(text=f"{title}", font_size="extraLarge")
        self.__write_to_entire_msg([block])
        self._add_actions(platform_enabled, finding, cluster_name, account_id)

        self.__write_to_entire_msg([MsTeamsTextBlock(text=f"**Source:** *{cluster_name}*")])

        if finding.description is not None:
            block = MsTeamsTextBlock(text=finding.description)
            self.__write_to_entire_msg([block])

    def _add_actions(self, platform_enabled: bool, finding: Finding, cluster_name: str, account_id: str):
        actions: list[str] = []
        if platform_enabled:  # add link to the Robusta ui, if it's configured
            actions.append(f"[🔎 Investigate]({finding.get_investigate_uri(account_id, cluster_name)})")

            if finding.add_silence_url:
                silence_url = finding.get_prometheus_silence_url(account_id, cluster_name)
                actions.append(f"[🔕 Silence]({silence_url})")

        for link in finding.links:
            link_url = link.url
            if link.type == LinkType.PROMETHEUS_GENERATOR_URL and self.prefer_redirect_to_platform and platform_enabled:
                link_url = convert_prom_graph_url_to_robusta_metrics_explorer(link.url, cluster_name, account_id)
            action: str = f"[{link.link_text}]({link_url})"
            actions.append(action)

        if actions:
            self.__write_to_entire_msg([MsTeamsTextBlock(text=" ".join(actions))])

    @classmethod
    def __build_msteams_title(
        cls, title: str, status: FindingStatus, severity: FindingSeverity, add_silence_url: bool
    ) -> str:
        status_str: str = f"{status.to_emoji()} {status.name.lower()} - " if add_silence_url else ""
        return f"{status_str}{severity.to_emoji()} {severity.name} - **{title}**"

    def write_current_section(self):
        if len(self.current_section) == 0:
            return

        space_block = MsTeamsTextBlock(text=" ", font_size="small")
        separator_block = MsTeamsTextBlock(text=" ", separator=True)

        underline_block = MsTeamsColumn()
        underline_block.add_column(items=[space_block, separator_block], width_stretch=True)

        self.__write_to_entire_msg([underline_block])
        self.__write_to_entire_msg(self.current_section)
        self.current_section = []

    def __write_to_entire_msg(self, blocks: List[MsTeamsBase]):
        self.entire_msg += blocks

    def __write_to_current_section(self, blocks: List[MsTeamsBase]):
        self.current_section += blocks

    def __sub_section_separator(self):
        if len(self.current_section) == 0:
            return
        space_block = MsTeamsTextBlock(text=" ", font_size="small")
        separator_block = MsTeamsTextBlock(text="_" * 30, font_size="small", horizontal_alignment="center")
        self.__write_to_current_section([space_block, separator_block, space_block, space_block])

    def upload_files(self, file_blocks: List[FileBlock]):
        msteams_files = MsTeamsAdaptiveCardFiles()
        block_list: List[MsTeamsBase] = msteams_files.upload_files(file_blocks)
        if len(block_list) > 0:
            self.__sub_section_separator()

        self.text_file_containers += msteams_files.get_text_files_containers_list()

        self.__write_to_current_section(block_list)

    def table(self, table_block: TableBlock):
        blocks: List[MsTeamsBase] = []
        if table_block.table_name:
            blocks.append(MsTeamsTextBlock(table_block.table_name))
        blocks.append(MsTeamsTable(list(table_block.headers), table_block.render_rows(), table_block.column_width))
        self.__write_to_current_section(blocks)

    def items_list(self, block: ListBlock):
        self.__sub_section_separator()
        for line in block.items:
            bullet_lines = "\n- " + line + "\n"
            self.__write_to_current_section([MsTeamsTextBlock(bullet_lines)])

    def diff(self, block: KubernetesDiffBlock):
        rows = [f"*{diff.formatted_path}*: {diff.other_value} -> {diff.value}" for diff in block.diffs]

        list_blocks = ListBlock(rows)
        self.items_list(list_blocks)

    def markdown_block(self, block: MarkdownBlock):
        if not block.text:
            return
        self.__write_to_current_section([MsTeamsTextBlock(block.text)])

    def divider_block(self):
        self.__write_to_current_section([MsTeamsTextBlock("\n\n")])

    def header_block(self, block: HeaderBlock):
        current_header_string = block.text + "\n\n"
        self.__write_to_current_section([MsTeamsTextBlock(current_header_string, font_size="large")])

    # dont include the base 64 images in the total size calculation
    def _put_text_files_data_up_to_max_limit(self, complete_card_map: map):
        curr_images_len = 0
        for element in self.entire_msg:
            if isinstance(element, MsTeamsImages):
                curr_images_len += element.get_images_len_in_bytes()

        max_len_left = self.MAX_SIZE_IN_BYTES - (self.__get_current_card_len(complete_card_map) - curr_images_len)

        curr_line = 0
        while True:
            line_added = False
            curr_line += 1
            for text_element, lines in self.text_file_containers:
                if len(lines) < curr_line:
                    continue

                line = lines[len(lines) - curr_line]
                max_len_left -= len(line)
                if max_len_left < 0:
                    return
                new_text_value = line + text_element.get_text_from_block()
                text_element.set_text_from_block(new_text_value)
                line_added = True

            if not line_added:
                return

    def send(self):
        try:
            complete_card_map: dict = MsTeamsCard(self.entire_msg).get_map_value()
            self._put_text_files_data_up_to_max_limit(complete_card_map)

            response = requests.post(self.webhook_url, json=complete_card_map)
            if response.status_code not in [200, 201]:
                logging.error(f"Error sending to ms teams json: {complete_card_map} error: {response.reason}")

            if response.text and "error" in response.text.lower():  # teams error indication is in the text only :(
                logging.error(f"Failed to send message to teams. error: {response.text} message: {complete_card_map}")

        except Exception as e:
            logging.error(f"error sending message to msteams\ne={e}\n")

    @classmethod
    def __get_current_card_len(cls, complete_card_map: dict):
        return len(json.dumps(complete_card_map, ensure_ascii=True, indent=2))
