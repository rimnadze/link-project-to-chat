class LinkProjectToChat < Formula
  include Language::Python::Virtualenv

  desc "Link a project directory to a Telegram bot that chats with Claude"
  homepage "https://github.com/rimnadze/link-project-to-chat"
  url "https://files.pythonhosted.org/packages/source/l/link-project-to-chat/link_project_to_chat-VERSION.tar.gz"
  sha256 "FILL_AFTER_PYPI_PUBLISH"
  license "MIT"

  depends_on "python@3.13"

  resource "python-telegram-bot" do
    url "https://files.pythonhosted.org/packages/source/p/python-telegram-bot/python_telegram_bot-22.0.tar.gz"
    sha256 "FILL"
  end

  resource "click" do
    url "https://files.pythonhosted.org/packages/source/c/click/click-8.1.8.tar.gz"
    sha256 "FILL"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "Usage:", shell_output("#{bin}/link-project-to-chat --help")
  end
end
